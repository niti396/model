# Copyright 2018 The TensorFlow Authors All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Script for training an RL agent using the UVF algorithm.

To run locally: See run_train.py
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import time
import tensorflow as tf
slim = tf.contrib.slim

import gin.tf
# pylint: disable=unused-import
import train_utils
import agent as agent_
from agents import circular_buffer
from utils import utils as uvf_utils
from environments import create_maze_env
from environments.simple_env import create_simple_env
from collections import OrderedDict
import boto3
import glob
# pylint: enable=unused-import


flags = tf.app.flags

FLAGS = flags.FLAGS
flags.DEFINE_string('goal_sample_strategy', 'sample',
                    'None, sample, FuN')

LOAD_PATH = None
# LOAD_PATH = "/Users/haoran/tmp/hiro_xy/ant_maze/base_uvf/20190223_160941/train/model.ckpt-4500"


def collect_experience(tf_env, agent, meta_agent, state_preprocess,
                       replay_buffer, meta_replay_buffer,
                       action_fn, meta_action_fn,
                       environment_steps, num_episodes, num_env_resets,
                       episode_rewards, episode_meta_rewards,
                       store_context,
                       disable_agent_reset,
                       store_meta_transition_every_n,
                       q_values,
                       meta_q_values):
  """Collect experience in a tf_env into a replay_buffer using action_fn.

  Takes a step in the env.
  Updates contexts.
  Put (state, action, reward, next_state, context, next_context) to low-level
    buffer.
  Put (starting_state, starting_action=goal, meta_reward, end_state,
    in_between_states, in_between_actions, meta_context, meta_next_context) to
    high-level buffer at the end of a meta-transition.
  Updates counters like episodes.

  Args:
    tf_env: A TFEnvironment.
    agent: A UVF agent.
    meta_agent: A Meta Agent.
    replay_buffer: A Replay buffer to collect experience in.
    meta_replay_buffer: A Replay buffer to collect meta agent experience in.
    action_fn: A function to produce actions given current state.
    meta_action_fn: A function to produce meta actions given current state.
    environment_steps: A variable to count the number of steps in the tf_env.
    num_episodes: A variable to count the number of episodes.
    num_env_resets: A variable to count the number of resets.
    store_context: A boolean to check if store context in replay.
    disable_agent_reset: A boolean that disables agent from resetting.

  Returns:
    A collect_experience_op that excute an action and store into the
    replay_buffers
  """
  tf_env.start_collect()
  state = tf_env.current_obs()
  state_repr = state_preprocess(state)
  action = action_fn(state, context=None)

  with tf.control_dependencies([state]):  # Make sure to not get next state
    time_step, reward, discount = tf_env.step(
        tf.expand_dims(action, 0))

  def increment_step():
    return environment_steps.assign_add(1)

  def increment_episode():
    return num_episodes.assign_add(1)

  def increment_env_reset():
    return num_env_resets.assign_add(1)


  def update_q_values(agent, meta_agent, state):
    # import ipdb; ipdb.set_trace()
    new_q_value = agent.value_net(
        agent._batch_state(
            agent.merged_state(state, context=None)))
    new_meta_q_value = meta_agent.value_net(
        meta_agent._batch_state(
            meta_agent.merged_state(state, context=None)))
    return tf.group(
      q_values.assign(tf.concat([new_q_value, q_values[:-1]], 0)),
      meta_q_values.assign(tf.concat([new_meta_q_value, meta_q_values[:-1]], 0))
    )

  def update_episode_rewards(context_reward, meta_reward, reset):
    # episode_rewards is an array to track total rewards from recent episodes.
    # The first one is always the current episode. The numbers are shifted
    # rightwards when a new episode starts.
    new_episode_rewards = tf.concat(
        [episode_rewards[:1] + context_reward, episode_rewards[1:]], 0)
    new_episode_meta_rewards = tf.concat(
        [episode_meta_rewards[:1] + meta_reward,
         episode_meta_rewards[1:]], 0)
    return tf.group(
        episode_rewards.assign(
            tf.cond(reset,
                    lambda: tf.concat([[0.], episode_rewards[:-1]], 0),
                    lambda: new_episode_rewards)),
        episode_meta_rewards.assign(
            tf.cond(reset,
                    lambda: tf.concat([[0.], episode_meta_rewards[:-1]], 0),
                    lambda: new_episode_meta_rewards)))

  # Check whether to increment step, episode, and env_reset counts
  def no_op_int():
    return tf.constant(0, dtype=tf.int64)

  step_cond = agent.step_cond_fn(state, action,
                                 time_step,
                                 environment_steps, num_episodes)
  reset_episode_cond = agent.reset_episode_cond_fn(
      state, action,
      time_step, environment_steps, num_episodes)
  reset_env_cond = agent.reset_env_cond_fn(state, action,
                                           time_step,
                                           environment_steps, num_episodes)

  increment_step_op = tf.cond(step_cond, increment_step, no_op_int)
  increment_episode_op = tf.cond(reset_episode_cond, increment_episode,
                                 no_op_int)
  increment_env_reset_op = tf.cond(reset_env_cond, increment_env_reset, no_op_int)
  increment_op = tf.group(increment_step_op, increment_episode_op,
                          increment_env_reset_op)

  # Get next state
  with tf.control_dependencies([increment_op, reward, discount]):
    next_state = tf_env.current_obs()
    next_state_repr = state_preprocess(next_state)
    # TODO: next_reset_episode_cond is probably wrong; remove it in the future
    # next_reset_episode_cond = tf.logical_or(
    #     agent.reset_episode_cond_fn(
    #         state, action,
    #         time_step, environment_steps, num_episodes),
    #     tf.equal(discount, 0.0))
    # next_state = tf.Print(next_state, [agent.tf_context.t, reset_env_cond, state, action, next_state], 'print2')
    # next_reset_episode_cond = tf.Print(next_reset_episode_cond, [next_reset_episode_cond], 'reset_epi')

  # Get next context
  if store_context:
    context = [tf.identity(var) + tf.zeros_like(var) for var in agent.context_vars]  # why add zeros???
    meta_context = [tf.identity(var) + tf.zeros_like(var) for var in meta_agent.context_vars]
  else:
    context = []
    meta_context = []

  # Compute context rewards
  # Copy agent.tf_context.t, because it will be incremented and potentially
  # reset to 0 during agent.cond_begin_episode_op_v2, which is just a mess.
  cur_t = tf.identity(agent.tf_context.t + 0)  # for unknown reasons the "+ 0" is important

  with tf.control_dependencies([next_state] + context + meta_context + [cur_t]):
    if disable_agent_reset:
      collect_experience_ops = [tf.no_op()]
    else:
      # Reset the agent at episode start. Othersie let the agent do whatever,
      # which for now is computing and storing context rewards.
      collect_experience_ops = agent.cond_begin_episode_op_v2(
          tf.logical_not(reset_episode_cond),
          [state, action, reward, next_state,
           state_repr, next_state_repr],
          mode='explore', meta_action_fn=meta_action_fn)
      # TODO: rename "cond_begin_episode_op" to "compute_rewards" or alike
      context_reward, meta_reward = collect_experience_ops
      collect_experience_ops = list(collect_experience_ops)
      collect_experience_ops.append(
          update_episode_rewards(tf.reduce_sum(context_reward), meta_reward,
                                 reset_episode_cond))
      collect_experience_ops.append(
          update_q_values(agent, meta_agent, state))

  # Compute transition and meta-transition, and put them in the buffers.
  meta_action_every_n = agent.tf_context.meta_action_every_n
  with tf.control_dependencies(collect_experience_ops):
    transition = [state, action, reward, discount, next_state]

    meta_action = tf.to_float(
        tf.concat(context, -1))  # Meta agent action is low-level context

    # IMPORTANT: Since agent.cond_begin_episode_op executes
    # agent.tf_context.step, which increments agent.tf_context.t, the timer
    # has come ahead of the current time.
    cur_period_ind = cur_t % meta_action_every_n
    prev_period_ind = (cur_t - 1) % meta_action_every_n

    if store_meta_transition_every_n is None:
      store_meta_transition_every_n = meta_action_every_n
    meta_end = tf.logical_and(
      tf.equal((cur_t - meta_action_every_n) % store_meta_transition_every_n, 0),
      tf.greater_equal(cur_t, meta_action_every_n))  # the history should only contain data from current traj
    with tf.variable_scope(tf.get_variable_scope(), reuse=tf.AUTO_REUSE):
      # states_var stores all states within a meta_period. New states are pushed
      # in a circular fashion.

      def _create_history_var(name, template):
        return tf.get_variable(
          name=name,
          shape=[meta_action_every_n] + template.shape.as_list(),
          dtype=template.dtype,
        )

      states_var = _create_history_var('states_var', state)
      actions_var = _create_history_var('actions_var', action)
      rewards_var = _create_history_var('rewards_var', reward)
      meta_actions_var = _create_history_var('meta_actions_var', meta_action)
      meta_contexts_var = [
        _create_history_var('meta_contexts_var%d' % idx, meta_context[idx])
        for idx in range(len(meta_context))
      ]

    # The meta_agent does not know agent's action until one step later, so
    # it makes up for the update now.
    actions_var_upd = tf.scatter_update(actions_var, prev_period_ind, action)
    with tf.control_dependencies([actions_var_upd]):
      actions = tf.identity(actions_var) + tf.zeros_like(actions_var)
      meta_reward = tf.identity(meta_reward) + tf.zeros_like(meta_reward)
      meta_reward = tf.reshape(meta_reward, reward.shape)

    reward = 0.1 * meta_reward  # why 0.1?

    # Note: grab history from (cur_t - c + 1) to cur_t
    past_states = tf.concat([states_var[cur_period_ind:], states_var[:cur_period_ind]], 0)
    past_actions = tf.concat([actions[cur_period_ind:], actions[:cur_period_ind]], 0)
    past_states.set_shape(states_var.shape)  # tell tf the shape since it can't do grade school math
    past_actions.set_shape(actions_var.shape)

    meta_transition = OrderedDict({  # Must be OrderedDict, since buffer.get_randomb_batch() doesn't return the keys
      'state': states_var[cur_period_ind],  # cur_period_id corresponds to time (cur_t - c + 1)
      'meta_action': meta_actions_var[cur_period_ind],
      'reward': tf.reduce_sum(rewards_var, axis=0) - rewards_var[cur_period_ind] + reward,  # Include the very last reward (since meta_transition is stored before rewards_var_upd is run
      'discount': discount * (1 - tf.to_float(reset_episode_cond)),
      'next_state': next_state,  # at time (cur_t + 1)
      'past_states': past_states,  # t ~ [cur_t - c + 1, cur_t]
      'past_actions': past_actions,  # t ~ [cur_t - c + 1, cur_t]
      'rewards': rewards_var,
      'meta_actions': meta_actions_var,
    })
    if store_context:  # store current and next context into replay
      transition += context + list(agent.context_vars)
      meta_contexts = [
        meta_contexts_var[idx][cur_period_ind]
        for idx in range(len(meta_context))]
      meta_transition.update(OrderedDict({
        'meta_context_%d' % i: v
        for i, v in enumerate(meta_contexts + list(meta_agent.context_vars))
      }))

    meta_step_cond = tf.squeeze(tf.logical_and(step_cond, tf.logical_or(reset_episode_cond, meta_end)))

    collect_experience_op = tf.group(
        replay_buffer.maybe_add(transition, step_cond),
        meta_replay_buffer.maybe_add(meta_transition, meta_step_cond),
    )

    # TODO: separate storing history in vars and how meta_transition is formed
    # meta_transition should always use data from [t-c, t]

  # Store
  with tf.control_dependencies([collect_experience_op]):
    collect_experience_op = tf.cond(reset_env_cond, tf_env.reset, tf.no_op)

  with tf.control_dependencies([collect_experience_op]):
    states_var_upd = tf.scatter_update(states_var, cur_period_ind, next_state)  # TODO: why next state instead of cur?
    rewards_var_upd = tf.scatter_update(rewards_var, cur_period_ind, reward)
    meta_action = tf.to_float(tf.concat(agent.context_vars, -1))
    meta_actions_var_upd = tf.scatter_update(
      meta_actions_var, cur_period_ind, meta_action)
    meta_contexts_var_upd = [
      tf.scatter_update(
        meta_contexts_var[idx],
        cur_period_ind,
        meta_agent.context_vars[idx],
      )
      for idx in range(len(meta_context))
    ]
  grab_from_graph = {
    'agent': agent,
    'meta_agent': meta_agent,
    'replay_buffer': replay_buffer,
    'meta_replay_buffer': meta_replay_buffer,
  }
  return tf.group(
      collect_experience_op,
      states_var_upd,
      rewards_var_upd,
      meta_actions_var_upd,
      *meta_contexts_var_upd), grab_from_graph


def sample_best_meta_actions(state_reprs, next_state_reprs, prev_meta_actions,
                             low_states, low_actions, low_state_reprs,
                             inverse_dynamics, uvf_agent, k=10):
  """Return meta-actions which approximately maximize low-level log-probs."""
  sampled_actions = inverse_dynamics.sample(state_reprs, next_state_reprs, k, prev_meta_actions)
  sampled_actions = tf.stop_gradient(sampled_actions)
  sampled_log_probs = tf.reshape(uvf_agent.log_probs(
      tf.tile(low_states, [k, 1, 1]),
      tf.tile(low_actions, [k, 1, 1]),
      tf.tile(low_state_reprs, [k, 1, 1]),
      [tf.reshape(sampled_actions, [-1, sampled_actions.shape[-1]])]),
                                 [k, low_states.shape[0],
                                  low_states.shape[1], -1])
  fitness = tf.reduce_sum(sampled_log_probs, [2, 3])
  best_actions = tf.argmax(fitness, 0)
  actions = tf.gather_nd(
      sampled_actions,
      tf.stack([best_actions,
                tf.range(prev_meta_actions.shape[0], dtype=tf.int64)], -1))
  return actions


@gin.configurable
def train_uvf(train_dir,
              environment=None,
              num_bin_actions=3,
              agent_class=None,
              meta_agent_class=None,
              state_preprocess_class=None,
              inverse_dynamics_class=None,
              exp_action_wrapper=None,
              replay_buffer=None,
              meta_replay_buffer=None,
              replay_num_steps=1,
              meta_replay_num_steps=1,
              critic_optimizer=None,
              actor_optimizer=None,
              meta_critic_optimizer=None,
              meta_actor_optimizer=None,
              repr_optimizer=None,
              relabel_contexts=False,
              meta_relabel_contexts=False,
              batch_size=64,
              repeat_size=0,
              num_episodes_train=2000,
              initial_episodes=2,
              initial_steps=None,
              num_updates_per_observation=1,
              num_collect_per_update=1,
              num_collect_per_meta_update=1,
              gamma=1.0,
              meta_gamma=1.0,
              reward_scale_factor=1.0,
              target_update_period=1,
              should_stop_early=None,
              clip_gradient_norm=0.0,
              summarize_gradients=False,
              debug_summaries=False,
              log_every_n_steps=100,
              prefetch_queue_capacity=2,
              policy_save_dir='policy',
              save_policy_every_n_steps=1000,
              save_policy_interval_secs=0,
              replay_context_ratio=0.0,
              next_state_as_context_ratio=0.0,
              state_index=0,
              zero_timer_ratio=0.0,
              timer_index=-1,
              debug=False,
              max_policies_to_save=None,
              max_steps_per_episode=None,
              load_path=LOAD_PATH,
              store_meta_transition_every_n=None):
  """Train an agent.

  Run init_collect_experience_op which fills the replay buffer with enough data.
  Then grab data from the buffer and run train_ops, which includes
    train_op: train actor, critic, state_preprocess, and updates the target.
    meta_train_op: train meta_actor, meta_critic, and updates the target.
    collect_experience_op:
    - takes one step in the env
    - updates the contexts
    - store the low-level transition
    - store the high-level (meta) transition every meta_action_every_n steps

  Then run train_step_fn, which includes logging, policy saving, termination
    checking, and training.

  """
  tf_env = create_maze_env.TFPyEnvironment(environment)
  observation_spec = [tf_env.observation_spec()]
  action_spec = [tf_env.action_spec()]

  max_steps_per_episode = max_steps_per_episode or tf_env.pyenv.max_episode_steps

  assert max_steps_per_episode, 'max_steps_per_episode need to be set'

  if initial_steps is None:
    initial_steps = initial_episodes * max_steps_per_episode

  if agent_class.ACTION_TYPE == 'discrete':
    assert False
  else:
    assert agent_class.ACTION_TYPE == 'continuous'

  assert agent_class.ACTION_TYPE == meta_agent_class.ACTION_TYPE
  with tf.variable_scope('meta_agent'):
    meta_agent = meta_agent_class(
        observation_spec,
        action_spec,
        tf_env,
        debug_summaries=debug_summaries)

  # TODO: the following line has no effect, since ReplaySampler is the only
  # one requiring it and it's never used.
  meta_agent.set_replay(replay=meta_replay_buffer)

  with tf.variable_scope('uvf_agent'):
    uvf_agent = agent_class(
        observation_spec,
        action_spec,
        tf_env,
        debug_summaries=debug_summaries)
    uvf_agent.set_meta_agent(agent=meta_agent)
    uvf_agent.set_replay(replay=replay_buffer)

  with tf.variable_scope('state_preprocess'):
    state_preprocess = state_preprocess_class()

  with tf.variable_scope('inverse_dynamics'):
    # Infer which goals could have led to the state transitions
    inverse_dynamics = inverse_dynamics_class(
        meta_agent.sub_context_as_action_specs[0])

  # Create counter variables
  global_step = tf.contrib.framework.get_or_create_global_step()
  num_episodes = tf.Variable(0, dtype=tf.int64, name='num_episodes')
  num_env_resets = tf.Variable(0, dtype=tf.int64, name='num_env_resets')
  num_updates = tf.Variable(0, dtype=tf.int64, name='num_updates')
  num_meta_updates = tf.Variable(0, dtype=tf.int64, name='num_meta_updates')
  episode_rewards = tf.Variable([0.] * 100, name='episode_rewards')  # TODO(why * 100?)
  episode_meta_rewards = tf.Variable([0.] * 100, name='episode_meta_rewards')
  low_level_q_values = tf.Variable([0.] * 100, name='low_level_q_values')
  high_level_q_values = tf.Variable([0.] * 100, name='high_level_q_values')

  # Create counter variables summaries
  train_utils.create_counter_summaries([
      ('environment_steps', global_step),
      ('num_episodes', num_episodes),
      ('num_env_resets', num_env_resets),
      ('num_updates', num_updates),
      ('num_meta_updates', num_meta_updates),
      ('replay_buffer_adds', replay_buffer.get_num_adds()),
      ('meta_replay_buffer_adds', meta_replay_buffer.get_num_adds()),
  ])

  tf.summary.scalar('avg_episode_rewards',
                    tf.reduce_mean(episode_rewards[1:]))  # TODO(why 1:?)
  tf.summary.scalar('avg_episode_meta_rewards',
                    tf.reduce_mean(episode_meta_rewards[1:]))
  tf.summary.scalar('avg_q_values',
                    tf.reduce_mean(low_level_q_values[1:]))
  tf.summary.scalar('avg_meta_q_values',
                    tf.reduce_mean(high_level_q_values[1:]))
  tf.summary.histogram('episode_rewards', episode_rewards[1:])
  tf.summary.histogram('episode_meta_rewards', episode_meta_rewards[1:])

  # Create init ops
  action_fn = uvf_agent.action
  action_fn = uvf_agent.add_noise_fn(action_fn, global_step=None)
  meta_action_fn = meta_agent.action
  meta_action_fn = meta_agent.add_noise_fn(meta_action_fn, global_step=None)
  # meta_actions_fn = meta_agent.actions
  # meta_actions_fn = meta_agent.add_noise_fn(meta_actions_fn, global_step=None)

  collect_experience_kwargs = dict(
    tf_env=tf_env,
    agent=uvf_agent,
    meta_agent=meta_agent,
    state_preprocess=state_preprocess,
    replay_buffer=replay_buffer,
    meta_replay_buffer=meta_replay_buffer,
    action_fn=action_fn,
    meta_action_fn=meta_action_fn,
    environment_steps=global_step,
    num_episodes=num_episodes,
    num_env_resets=num_env_resets,
    episode_rewards=episode_rewards,
    episode_meta_rewards=episode_meta_rewards,
    store_context=True,
    disable_agent_reset=False,
    store_meta_transition_every_n=store_meta_transition_every_n,
    q_values = low_level_q_values,
    meta_q_values = high_level_q_values)

  init_collect_experience_op, _ = collect_experience(**collect_experience_kwargs)

  # Create train ops
  collect_experience_op, grab_from_graph = collect_experience(**collect_experience_kwargs)

  train_op_list = []
  repr_train_op = tf.constant(0.0)
  for mode in ['meta', 'nometa']:  # literally alternate btw high and low levels
    if mode == 'meta':
      agent = meta_agent
      buff = meta_replay_buffer
      critic_opt = meta_critic_optimizer
      actor_opt = meta_actor_optimizer
      relabel = meta_relabel_contexts
      num_steps = meta_replay_num_steps
      my_gamma = meta_gamma,
      n_updates = num_meta_updates
    else:
      agent = uvf_agent
      buff = replay_buffer
      critic_opt = critic_optimizer
      actor_opt = actor_optimizer
      relabel = relabel_contexts
      num_steps = replay_num_steps
      my_gamma = gamma
      n_updates = num_updates

    with tf.name_scope(mode):
      batch = list(buff.get_random_batch(batch_size, num_steps=num_steps).values())
      # TODO: use namedtuple instead of hard indexing
      # Grab rewards from the batch. Scale and record them.
      states, actions, rewards, discounts, next_states = batch[:5]
      with tf.name_scope('Reward'):
        tf.summary.scalar('average_step_reward', tf.reduce_mean(rewards))
      rewards *= reward_scale_factor
      # TODO: what does the prefetch_queue do?
      batch_queue = slim.prefetch_queue.prefetch_queue(
          [states, actions, rewards, discounts, next_states] + batch[5:],
          capacity=prefetch_queue_capacity,
          name='batch_queue')

      batch_dequeue = batch_queue.dequeue()
      # Duplicate the batch by repeat_size times
      if repeat_size > 0:
        batch_dequeue = [
            tf.tile(batch, (repeat_size+1,) + (1,) * (batch.shape.ndims - 1))
            for batch in batch_dequeue
        ]
        batch_size *= (repeat_size + 1)
      states, actions, rewards, discounts, next_states = batch_dequeue[:5]
      if mode == 'meta':
        low_states = batch_dequeue[5]  # [B,T,...]?
        low_actions = batch_dequeue[6]
        low_state_reprs = state_preprocess(low_states)
      state_reprs = state_preprocess(states)
      next_state_reprs = state_preprocess(next_states)

      if mode == 'meta':  # Re-label meta-action
        prev_actions = actions
        if FLAGS.goal_sample_strategy == 'None':
          pass
        elif FLAGS.goal_sample_strategy == 'FuN':
          # Re-sample meta-actions from N(s_{t+c} - s_t, ?)
          actions = inverse_dynamics.sample(state_reprs, next_state_reprs, 1, prev_actions, sc=0.1)
          actions = tf.stop_gradient(actions)
        elif FLAGS.goal_sample_strategy == 'sample':
          # Re-sample meta-actions from N(s_{t+c} - s_t, ?) and pick g_t
          # that maximize \sum_{i=1}^c \log \pi^{low}(a_{t+i} | s_{t+i}, g_{t+i})
          # where g_{t+i} = s_t + g_t - s_{t+i}
          # k: number of resamples, not goal_horizon
          actions = sample_best_meta_actions(state_reprs, next_state_reprs, prev_actions,
                                             low_states, low_actions, low_state_reprs,
                                             inverse_dynamics, uvf_agent, k=10)
        else:
          assert False

      if state_preprocess.trainable and mode == 'meta':
        # Representation learning is based on meta-transitions, but is trained
        # along with low-level policy updates.

        # repr_loss is KL btw actual dynamics and the imagined energy-based
        # dynamics exp(-D(s_{t+i}, g_t)) on the repr space
        repr_loss, _, _ = state_preprocess.loss(states, next_states, low_actions, low_states)
        repr_train_op = slim.learning.create_train_op(
            repr_loss,
            repr_optimizer,
            global_step=None,
            update_ops=None,
            summarize_gradients=summarize_gradients,
            clip_gradient_norm=clip_gradient_norm,
            variables_to_train=state_preprocess.get_trainable_vars(),)

      # Get contexts for training
      contexts, next_contexts = agent.sample_contexts(
          mode='train', batch_size=batch_size,
          state=states, next_state=next_states,
      )
      # TODO:
      if not relabel:  # Re-label context (in the style of TDM or HER).
        contexts, next_contexts = (
            batch_dequeue[-2*len(contexts):-1*len(contexts)],
            batch_dequeue[-1*len(contexts):])

      merged_states = agent.merged_states(states, contexts)
      merged_next_states = agent.merged_states(next_states, next_contexts)
      if mode == 'nometa':  # goal-completion rewards
        context_rewards, context_discounts = agent.compute_rewards(
            state_reprs, actions, rewards, next_state_reprs, contexts)
      elif mode == 'meta': # Meta-agent uses sum of rewards, not context-specific rewards.
        _, context_discounts = agent.compute_rewards(
            states, actions, rewards, next_states, contexts)
        context_rewards = rewards

      # Multiply discounts by values from contexts
      if agent.gamma_index is not None:
        context_discounts *= tf.cast(
            tf.reshape(contexts[agent.gamma_index], (-1,)),
            dtype=context_discounts.dtype)
      else: context_discounts *= my_gamma

      critic_loss = agent.critic_loss(merged_states, actions,
                                      context_rewards, context_discounts,
                                      merged_next_states)

      critic_loss = tf.reduce_mean(critic_loss)

      # DDPG calls the critic to compute actor_loss
      actor_loss = agent.actor_loss(merged_states, actions,
                                    context_rewards, context_discounts,
                                    merged_next_states)
      actor_loss *= tf.to_float(  # Only update actor every N steps.
          tf.equal(n_updates % target_update_period, 0))

      critic_train_op = slim.learning.create_train_op(
          critic_loss,
          critic_opt,
          global_step=n_updates,
          update_ops=None,
          summarize_gradients=summarize_gradients,
          clip_gradient_norm=clip_gradient_norm,
          variables_to_train=agent.get_trainable_critic_vars(),)
      critic_train_op = uvf_utils.tf_print(
          critic_train_op, [critic_train_op],
          message='critic_loss',
          print_freq=1000,
          name='critic_loss')
      train_op_list.append(critic_train_op)
      if actor_loss is not None:
        actor_train_op = slim.learning.create_train_op(
            actor_loss,
            actor_opt,
            global_step=None,
            update_ops=None,
            summarize_gradients=summarize_gradients,
            clip_gradient_norm=clip_gradient_norm,
            variables_to_train=agent.get_trainable_actor_vars(),)
        actor_train_op = uvf_utils.tf_print(
            actor_train_op, [actor_train_op],
            message='actor_loss',
            print_freq=1000,
            name='actor_loss')
        train_op_list.append(actor_train_op)

  assert len(train_op_list) == 4  # meta critic, meta actor, critic, actor
  # Train current actor & critic and then update the targets.
  # Why call the training ops again after updating the targets?
  with tf.control_dependencies(train_op_list[2:]):  # TODO: use dict instead of hard indexing
    update_targets_op = uvf_utils.periodically(
        uvf_agent.update_targets, target_update_period, 'update_targets')
  if meta_agent is not None:
    with tf.control_dependencies(train_op_list[:2]):
      update_meta_targets_op = uvf_utils.periodically(
          meta_agent.update_targets, target_update_period, 'update_targets')

  assert_op = tf.Assert(  # Hack to get training to stop.
      tf.less_equal(global_step, 200 + num_episodes_train * max_steps_per_episode),
      [global_step])
  with tf.control_dependencies([update_targets_op, assert_op]):
    train_op = tf.add_n(train_op_list[2:], name='post_update_targets')
    # Representation training steps on every low-level policy training step.
    train_op += repr_train_op
  with tf.control_dependencies([update_meta_targets_op, assert_op]):
    meta_train_op = tf.add_n(train_op_list[:2],
                             name='post_update_meta_targets')

  if debug_summaries:
    train_utils.gen_debug_batch_summaries(batch)
    slim.summaries.add_histogram_summaries(
        uvf_agent.get_trainable_critic_vars(), 'critic_vars')
    slim.summaries.add_histogram_summaries(
        uvf_agent.get_trainable_actor_vars(), 'actor_vars')

  # Alternates between several experience collection steps and training steps
  train_ops = train_utils.TrainOps(train_op, meta_train_op,
                                   collect_experience_op)

  policy_save_path = os.path.join(train_dir, policy_save_dir, 'model.ckpt')
  policy_vars = uvf_agent.get_actor_vars() + meta_agent.get_actor_vars() + [
      global_step, num_episodes, num_env_resets
  ] + list(uvf_agent.context_vars) + list(meta_agent.context_vars) + state_preprocess.get_trainable_vars()
  # add critic vars, since some test evaluation depends on them
  policy_vars += uvf_agent.get_trainable_critic_vars() + meta_agent.get_trainable_critic_vars()
  policy_saver = tf.train.Saver(
      policy_vars, max_to_keep=max_policies_to_save, sharded=False)

  lowlevel_vars = (uvf_agent.get_actor_vars() +
                   uvf_agent.get_trainable_critic_vars() +
                   state_preprocess.get_trainable_vars())
  lowlevel_saver = tf.train.Saver(lowlevel_vars)

  def policy_save_fn(sess):
    policy_saver.save(
        sess, policy_save_path, global_step=global_step, write_meta_graph=False)
    if save_policy_interval_secs > 0:
      tf.logging.info(
          'Wait %d secs after save policy.' % save_policy_interval_secs)
      time.sleep(save_policy_interval_secs)

  train_step_fn = train_utils.TrainStep(
      max_number_of_steps=num_episodes_train * max_steps_per_episode + 100,
      num_updates_per_observation=num_updates_per_observation,
      num_collect_per_update=num_collect_per_update,
      num_collect_per_meta_update=num_collect_per_meta_update,
      log_every_n_steps=log_every_n_steps,
      policy_save_fn=policy_save_fn,
      save_policy_every_n_steps=save_policy_every_n_steps,
      should_stop_early=should_stop_early,
      grab_from_graph=grab_from_graph,
      debug=debug,
  ).train_step

  local_init_op = tf.local_variables_initializer()
  init_targets_op = tf.group(uvf_agent.update_targets(1.0),
                             meta_agent.update_targets(1.0))

  def initialize_training_fn(sess):
    """Initialize training function."""
    sess.run(local_init_op)
    sess.run(init_targets_op)
    if load_path:
      tf.logging.info('Restoring low-level from %s' % load_path)
      lowlevel_saver.restore(sess, load_path)
    global_step_value = sess.run(global_step)
    assert global_step_value == 0, 'Global step should be zero.'
    collect_experience_call = sess.make_callable(
        init_collect_experience_op)

    for _ in range(initial_steps):
      collect_experience_call()

  train_saver = tf.train.Saver(max_to_keep=2, sharded=True)
  tf.logging.info('train dir: %s', train_dir)

  # Prevent GPU from using all memory
  session_config = tf.ConfigProto()
  session_config.gpu_options.allow_growth = True

  return slim.learning.train(
      train_ops,
      train_dir,
      train_step_fn=train_step_fn,  # (sess, train_op, global_step, train_step_kwargs)
      save_interval_secs=FLAGS.save_interval_secs,
      saver=train_saver,
      log_every_n_steps=0,
      global_step=global_step,
      master="",
      is_chief=(FLAGS.task == 0),
      save_summaries_secs=FLAGS.save_summaries_secs,
      init_fn=initialize_training_fn,
      session_config=session_config)

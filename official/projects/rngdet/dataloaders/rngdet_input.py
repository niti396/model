# Copyright 2023 The TensorFlow Authors. All Rights Reserved.
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

"""COCO data loader for Pix2Seq."""

from typing import Tuple
import tensorflow as tf
import math
import numpy as np
from official.vision.dataloaders import decoder
from official.vision.dataloaders import parser
from official.vision.ops import preprocess_ops
from official.projects.rngdet.dataloaders import sampler as rngdet_sampler


class Decoder(decoder.Decoder):
  """A tf.Example decoder for RNGDet."""

  def __init__(self):
    
    self._keys_to_features = {
    "sat_roi": tf.io.VarLenFeature(tf.int64),
    "label_masks_roi": tf.io.VarLenFeature(tf.int64),
    "historical_roi": tf.io.VarLenFeature(tf.int64),
    "gt_probs": tf.io.VarLenFeature(tf.float32),
    "gt_coords": tf.io.VarLenFeature(tf.float32),
    "list_len": tf.io.FixedLenFeature((), tf.int64),
    "gt_masks": tf.io.VarLenFeature(tf.int64),
    }

  def decode(self, serialized_example):
    parsed_tensors = tf.io.parse_single_example(
        serialized=serialized_example, features=self._keys_to_features)
    for k in parsed_tensors:
      if isinstance(parsed_tensors[k], tf.SparseTensor):
        if parsed_tensors[k].dtype == tf.string:
          parsed_tensors[k] = tf.sparse.to_dense(
              parsed_tensors[k], default_value='')
        else:
          parsed_tensors[k] = tf.sparse.to_dense(
              parsed_tensors[k], default_value=0)
    decoded_tensors = {
        'sat_roi': parsed_tensors['sat_roi'],
        'label_masks_roi': parsed_tensors['label_masks_roi'],
        'historical_roi': parsed_tensors['historical_roi'],
        'gt_probs': parsed_tensors['gt_probs'],
        'gt_coords': parsed_tensors['gt_coords'],
        'list_len': parsed_tensors['list_len'],
        'gt_masks': parsed_tensors['gt_masks']
    }
    
    return decoded_tensors


class Parser(parser.Parser):
  """Parse an image and its annotations into a dictionary of tensors."""

  def __init__(
      self,
      roi_size: int = 128,
      num_queries: int = 10
  ):
    self._roi_size = roi_size
    self._num_queries = num_queries
    
  def parse_fn(self, is_training):
    """Returns a parse fn that reads and parses raw tensors from the decoder.

    Args:
      is_training: a `bool` to indicate whether it is in training mode.

    Returns:
      parse: a `callable` that takes the serialized example and generate the
        images, labels tuple where labels is a dict of Tensors that contains
        labels.
    """
    def parse(decoded_tensors):
      """Parses the serialized example data."""
      if is_training:
        return self._parse_train_data(decoded_tensors)
      else:
        return self._parse_eval_data(decoded_tensors)

    return parse
  
  def _parse_train_data(self, data):
    """Parses data for training and evaluation."""
    sat_roi = tf.reshape(data['sat_roi'], [self._roi_size, self._roi_size, 3])
    label_masks_roi = tf.reshape(
        data['label_masks_roi'], [self._roi_size, self._roi_size, 2])
    historical_roi = tf.reshape(
        data['historical_roi'], [self._roi_size, self._roi_size, 1])
    gt_coords = tf.reshape(data['gt_coords'], [self._num_queries, 2])
    gt_masks = tf.reshape(
        data['gt_masks'], [self._roi_size, self._roi_size, self._num_queries])
    
    sat_roi = tf.image.convert_image_dtype(sat_roi, dtype=tf.float32)
    sat_roi = sat_roi * (
        0.7 + 0.3 * tf.random.uniform([], minval=0, maxval=1, dtype=tf.float32))
    rot_index = tf.random.uniform(shape=(), minval=0, maxval=4, dtype=tf.int32)
    theta = tf.cast(rot_index, tf.float32) * math.pi / 2
    # (gunho) original R matrix in incorrect
    #R = np.array([[np.cos(theta),np.sin(theta)],[np.sin(-theta),np.cos(theta)]])
    R = np.array([[np.cos(theta),-np.sin(theta)],[np.sin(theta),np.cos(theta)]])
    gt_coords = tf.transpose(tf.linalg.matmul(R, gt_coords, transpose_b=True))
    
    label_masks_roi = tf.image.rot90(label_masks_roi, rot_index)/255
    historical_roi = tf.image.rot90(historical_roi, rot_index)/255
    sat_roi = tf.image.rot90(sat_roi, rot_index)
    gt_masks = tf.image.rot90(gt_masks, rot_index)/255

    images = {
        'sat_roi': sat_roi,
        'historical_roi': historical_roi,
    }
    labels = {
        'label_masks_roi': label_masks_roi,
        'gt_probs': data['gt_probs'],
        'gt_coords': gt_coords,
        'list_len': data['list_len'],
        'gt_masks': gt_masks,
    }

    return images, labels
  
  def _parse_eval_data(self, data):
    """Parses data for training and evaluation."""

    # Gets original image.
    image = data['image']

    # Normalizes image with mean and std pixel values.
    image = tf.image.convert_image_dtype(image, dtype=tf.float32)

    image, image_info = preprocess_ops.resize_and_crop_image(
        image,
        self._output_size,
        padded_size=self._output_size,
        aug_scale_min=self._aug_scale_min,
        aug_scale_max=self._aug_scale_max)

    return image
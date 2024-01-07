import os
import numpy as np
import tensorflow as tf
from object_detection.utils import config_util
from object_detection.utils import visualization_utils as viz_utils
from object_detection.builders import model_builder
import utils
import cv2, glob

#EfficientDet D0チェックポイントをダウンロード
obj_det_path = '/worker/BboxSuggestion/research/object_detection'
if not os.path.isdir(obj_det_path):
    utils.download_ckpt()
pipeline_config = f'{obj_det_path}/test_data/efficientdet_d0_coco17_tpu-32/pipeline.config'
model_dir = f'{obj_det_path}/test_data/efficientdet_d0_coco17_tpu-32/checkpoint'
if not os.path.isfile(pipeline_config): assert False

# label_mapへのパス文字列を置換
utils.replace(pipeline_config, "PATH_TO_BE_CONFIGURED/label_map.txt", f"{obj_det_path}/data/mscoco_label_map.pbtxt")

# Load pipeline config and build a detection model
configs = config_util.get_configs_from_pipeline_file(pipeline_config)
model_config = configs['model']
detection_model = model_builder.build(
      model_config=model_config, is_training=False)

# Restore checkpoint
ckpt = tf.compat.v2.train.Checkpoint(
      model=detection_model)
ckpt.restore(os.path.join(model_dir, 'ckpt-0')).expect_partial()

def get_model_detection_function(model):
  """Get a tf.function for detection."""

  @tf.function
  def detect_fn(image):
    """Detect objects in image."""

    image, shapes = model.preprocess(image)
    prediction_dict = model.predict(image, shapes)
    detections = model.postprocess(prediction_dict, shapes)

    return detections, prediction_dict, tf.reshape(shapes, [-1])

  return detect_fn

detect_fn = get_model_detection_function(detection_model)

category_index, label_map_dict = utils.load_labelmap(configs)

image_dir = f'{obj_det_path}/test_images'

for image_path in glob.glob(f'{image_dir}/*.jpg'):

    image_np = utils.load_image_into_numpy_array(image_path)


    input_tensor = tf.convert_to_tensor(
        np.expand_dims(image_np, 0), dtype=tf.float32)
    detections, predictions_dict, shapes = detect_fn(input_tensor)

    label_id_offset = 1
    image_np_with_detections = image_np.copy()

    # Use keypoints if available in detections
    keypoints, keypoint_scores = None, None
    if 'detection_keypoints' in detections:
        keypoints = detections['detection_keypoints'][0].numpy()
        keypoint_scores = detections['detection_keypoint_scores'][0].numpy()

    viz_utils.visualize_boxes_and_labels_on_image_array(
        image_np_with_detections,
        detections['detection_boxes'][0].numpy(),
        (detections['detection_classes'][0].numpy() + label_id_offset).astype(int),
        detections['detection_scores'][0].numpy(),
        category_index,
        use_normalized_coordinates=True,
        max_boxes_to_draw=200,
        min_score_thresh=.30,
        agnostic_mode=False,
        keypoints=keypoints,
        keypoint_scores=keypoint_scores,
        keypoint_edges=utils.get_keypoint_tuples(configs['eval_config']))

    cv2.cvtColor(image_np_with_detections, cv2.COLOR_BGR2RGB, image_np_with_detections)
    output_name = os.path.basename(image_path)
    cv2.imwrite(output_name, image_np_with_detections)


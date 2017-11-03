import numpy as np
import os
import six.moves.urllib as urllib
import sys
import tarfile
import tensorflow as tf
from matplotlib import pyplot as plt
from PIL import Image
import time

#
# This is needed to display the images.
# %matplotlib inline

# This is needed since the notebook is stored in the object_detection folder.
sys.path.append("..")

from utils import label_map_util
from utils import visualization_utils as vis_util

# What model to download.
# MODEL_NAME = 'models.faster_rcnn_resnet101_coco_11_06_2017'
# MODEL_FILE = MODEL_NAME + '.tar.gz'
# DOWNLOAD_BASE = 'http://download.tensorflow.org/models/object_detection/'

# Path to frozen detection graph. This is the actual model that is used for the object detection.
PATH_TO_CKPT = r'east_ic_graph\frozen_inference_graph.pb'

# List of the strings that is used to add correct label for each box.
PATH_TO_LABELS = r"data\logo_label_map.pbtxt"

NUM_CLASSES = 2

# load tensorflow into memory
detection_graph = tf.Graph()
with detection_graph.as_default():
    od_graph_def = tf.GraphDef()
    with tf.gfile.GFile(PATH_TO_CKPT, 'rb') as fid:
        serialized_graph = fid.read()
        od_graph_def.ParseFromString(serialized_graph)
        tf.import_graph_def(od_graph_def, name='')
print("load tensorflow model to memery over")

# load label map
label_map = label_map_util.load_labelmap(PATH_TO_LABELS)
categories = label_map_util.convert_label_map_to_categories(label_map, max_num_classes=NUM_CLASSES,
                                                            use_display_name=True)
category_index = label_map_util.create_category_index(categories)


# help code
def load_image_into_numpy_array(image):
    (im_width, im_height) = image.size
    return np.array(image.getdata()).reshape(
        (im_height, im_width, 3)).astype(np.uint8)


# detection
# For the sake of simplicity we will use only 2 images:
# image1.jpg
# image2.jpg
# If you want to test the code with your images, just add path to the images to the TEST_IMAGE_PATHS.
PATH_TO_TEST_IMAGES_DIR = r'C:\Users\sunhongzhi\Desktop\deep32'

print(PATH_TO_TEST_IMAGES_DIR)
# Size, in inches, of the output images.
IMAGE_SIZE = (12, 8)

start_time = time.time()

with detection_graph.as_default():
    with tf.Session(graph=detection_graph) as sess:
        # Definite input and output Tensors for detection_graph
        image_tensor = detection_graph.get_tensor_by_name('image_tensor:0')
        # Each box represents a part of the image where a particular object was detected.
        detection_boxes = detection_graph.get_tensor_by_name('detection_boxes:0')
        # Each score represent how level of confidence for each of the objects.
        # Score is shown on the result image, together with the class label.
        detection_scores = detection_graph.get_tensor_by_name('detection_scores:0')
        detection_classes = detection_graph.get_tensor_by_name('detection_classes:0')
        num_detections = detection_graph.get_tensor_by_name('num_detections:0')
        total_count = 0
        error_count = 0
        for file_name in os.listdir(PATH_TO_TEST_IMAGES_DIR):
            image_path = os.path.join(PATH_TO_TEST_IMAGES_DIR, file_name)
            if image_path.lower().endswith(".jpg"):
                with Image.open(image_path) as image:
                    total_count = total_count + 1
                    # the array based representation of the image will be used later in order to prepare the
                    # result image with boxes and labels on it.
                    print("path = " + image_path)
                    image_np = load_image_into_numpy_array(image)
                    # Expand dimensions since the model expects images to have shape: [1, None, None, 3]
                    image_np_expanded = np.expand_dims(image_np, axis=0)
                    # Actual detection.
                    (boxes, scores, classes, num) = sess.run(
                        [detection_boxes, detection_scores, detection_classes, num_detections],
                        feed_dict={image_tensor: image_np_expanded})
                    # Visualization of the results of a detection.
                    classes_result = vis_util.getClassesResult(
                        image_np,
                        np.squeeze(boxes),
                        np.squeeze(classes).astype(np.int32),
                        np.squeeze(scores),
                        category_index,
                        use_normalized_coordinates=True,
                        line_thickness=8)
                    print("image path = " + image_path + ",classes_result = ", classes_result, ",total_count = ",
                          total_count)
                    if classes_result is None:
                        image.save(os.path.join(r'C:\Users\sunhongzhi\Desktop\test_error', file_name))
                        error_count = error_count + 1
    print(" total_count= ", total_count)
    print(" error_count= ", error_count)
    print(" used time = ", (time.time() - start_time))

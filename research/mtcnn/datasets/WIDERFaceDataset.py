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

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import numpy as np
import cv2
import numpy.random as npr
from utils.IoU import IoU

class WIDERFaceDataset(object):

	def __init__(self, name='WIDERFace'):
		self._name = name
		self._is_valid = False
		self._data = dict()

	@classmethod
	def positive_file_name(cls, target_root_dir):
		positive_file_name = os.path.join(target_root_dir, 'positive.txt')
		return(positive_file_name)

	@classmethod
	def part_file_name(cls, target_root_dir):
		part_file_name = os.path.join(target_root_dir, 'part.txt')
		return(part_file_name)

	@classmethod
	def negative_file_name(cls, target_root_dir):
		negative_file_name = os.path.join(target_root_dir, 'negative.txt')
		return(negative_file_name)

	def is_valid(self):
		return(self._is_valid)

	def data(self):
		return(self._data)

	def read_annotation(self, annotation_image_dir, annotation_file_name):
		
		if(not os.path.isfile(annotation_file_name)):
			return(False)

		self._data = dict()
		self._is_valid = False

		images = []
		bounding_boxes = []
		annotation_file = open(annotation_file_name, 'r')
		while( True ):
       			image_path = annotation_file.readline().strip('\n')
       			if( not image_path ):
       				break			

       			image_path = os.path.join(annotation_image_dir, image_path)
			image = cv2.imread(image_path)
			if(image is None):
				continue

       			images.append(image_path)

       			nums = annotation_file.readline().strip('\n')
       			one_image_boxes = []
       			for face_index in range(int(nums)):
       				bounding_box_info = annotation_file.readline().strip('\n').split(' ')

       				face_box = [float(bounding_box_info[i]) for i in range(4)]

       				xmin = face_box[0]
       				ymin = face_box[1]
       				xmax = xmin + face_box[2]
       				ymax = ymin + face_box[3]

       				one_image_boxes.append([xmin, ymin, xmax, ymax])

       			bounding_boxes.append(one_image_boxes)

		if(len(images)):			
			self._data['images'] = images
			self._data['bboxes'] = bounding_boxes
			self._is_valid = True
		else:
			self._is_valid = False

		return(self.is_valid())


	def generate(self, annotation_image_dir, annotation_file_name, minimum_face, target_root_dir):

		positive_dir = os.path.join(target_root_dir, 'positive')
		part_dir = os.path.join(target_root_dir, 'part')
		negative_dir = os.path.join(target_root_dir, 'negative')

		if(not os.path.exists(positive_dir)):
    			os.makedirs(positive_dir)
		if(not os.path.exists(part_dir)):
    			os.makedirs(part_dir)
		if(not os.path.exists(negative_dir)):
    			os.makedirs(negative_dir)

		positive_file = open(WIDERFaceDataset.positive_file_name(target_root_dir), 'w')
		part_file = open(os.path.join(target_root_dir, 'part.txt'), 'w')
		negative_file = open(os.path.join(target_root_dir, 'negative.txt'), 'w')
		with open(annotation_file_name, 'r') as f:
			annotations = f.readlines()

		num = len(annotations)

		positive_images = 0
		part_images = 0
		negative_images = 0
		annotation_number = 0

		for annotation in annotations:
    			annotation = annotation.strip().split(' ')			
			image_path = annotation[0]

			bbox = map(float, annotation[1:])
			boxes = np.array(bbox, dtype=np.float32).reshape(-1, 4)

			current_image = cv2.imread(os.path.join(annotation_image_dir, image_path + '.jpg'))
    			height, width, channel = current_image.shape

		    	annotation_number += 1        

			neg_num = 0
			while(neg_num < 50):
				size = npr.randint(12, min(width, height) / 2)
			        nx = npr.randint(0, width - size)
        			ny = npr.randint(0, height - size)
        
        			crop_box = np.array([nx, ny, nx + size, ny + size])
        
        			current_IoU = IoU(crop_box, boxes)
        
        			cropped_image = current_image[ny : ny + size, nx : nx + size, :]
        			resized_image = cv2.resize(cropped_image, (minimum_face, minimum_face), interpolation=cv2.INTER_LINEAR)
				if( np.max(current_IoU) < 0.3 ):
					file_path = os.path.join(negative_dir, "%s.jpg"%negative_images)
					negative_file.write(file_path + ' 0\n')
					cv2.imwrite(file_path, resized_image)
            				negative_images += 1
            				neg_num += 1

			for box in boxes:
				x1, y1, x2, y2 = box
				w = x2 - x1 + 1
				h = y2 - y1 + 1

				if( max(w, h) < 40 or x1 < 0 or y1 < 0 ):
            				continue

				for i in range(5):
			            	size = npr.randint(12, min(width, height) / 2)
            				delta_x = npr.randint(max(-size, -x1), w)
            				delta_y = npr.randint(max(-size, -y1), h)
            				nx1 = int(max(0, x1 + delta_x))
            				ny1 = int(max(0, y1 + delta_y))
            				if nx1 + size > width or ny1 + size > height:
                				continue
            				crop_box = np.array([nx1, ny1, nx1 + size, ny1 + size])
            				current_IoU = IoU(crop_box, boxes)
    
            				cropped_image = current_image[ny1: ny1 + size, nx1: nx1 + size, :]
            				resized_image = cv2.resize(cropped_image, (minimum_face, minimum_face), interpolation=cv2.INTER_LINEAR)
    
            				if np.max(current_IoU) < 0.3:
                				file_path = os.path.join(negative_dir, "%s.jpg" % negative_images)
                				negative_file.write(file_path + ' 0\n')
                				cv2.imwrite(file_path, resized_image)
                				negative_images += 1 

				for i in range(20):
            				size = npr.randint(int(min(w, h) * 0.8), np.ceil(1.25 * max(w, h)))

            				delta_x = npr.randint(-w * 0.2, w * 0.2)
            				delta_y = npr.randint(-h * 0.2, h * 0.2)

            				nx1 = int(max(x1 + w / 2 + delta_x - size / 2, 0))
            				ny1 = int(max(y1 + h / 2 + delta_y - size / 2, 0))
            				nx2 = nx1 + size
            				ny2 = ny1 + size

            				if nx2 > width or ny2 > height:
                				continue 
            				crop_box = np.array([nx1, ny1, nx2, ny2])
            				offset_x1 = (x1 - nx1) / float(size)
					offset_y1 = (y1 - ny1) / float(size)
            				offset_x2 = (x2 - nx2) / float(size)
            				offset_y2 = (y2 - ny2) / float(size)

            				cropped_image = current_image[ny1 : ny2, nx1 : nx2, :]
            				resized_image = cv2.resize(cropped_image, (minimum_face, minimum_face), interpolation=cv2.INTER_LINEAR)

            				box_ = box.reshape(1, -1)
            				if IoU(crop_box, box_) >= 0.65:
                				file_path = os.path.join(positive_dir, "%s.jpg"%positive_images)
                				positive_file.write(file_path + ' 1 %.2f %.2f %.2f %.2f\n'%(offset_x1, offset_y1, offset_x2, offset_y2))
                				cv2.imwrite(file_path, resized_image)
                				positive_images += 1
            				elif IoU(crop_box, box_) >= 0.4:
                				file_path = os.path.join(part_dir, "%s.jpg"%part_images)
                				part_file.write(file_path + ' -1 %.2f %.2f %.2f %.2f\n'%(offset_x1, offset_y1, offset_x2, offset_y2))
                				cv2.imwrite(file_path, resized_image)
                				part_images += 1

				if(annotation_number % 1000 == 0 ):
					print('%s number of images are done - positive - %s,  part - %s, negative - %s' % (annotation_number, positive_images, part_images, negative_images))

		negative_file.close()
		part_file.close()
		positive_file.close()

		return(True)


# yolo code from https://github.com/eriklindernoren/PyTorch-YOLOv3

import pickle
import numpy as np

class VehicleDetector:
    wantsRGB = True
    def __init__(self):
        self.frameNumber = 0

    def getFeatures(self, frame):
        bboxes = self.bboxes[self.frameNumber]
        self.frameNumber = (self.frameNumber + 1) % len(self.bboxes)
        segmentations = []
        return bboxes, segmentations

    def loadFeaturesFromDisk(self, featuresPath):
        assert(self.frameNumber==0)
        with open(featuresPath, 'rb') as file:
            self.bboxes = pickle.load(file)[0][0]
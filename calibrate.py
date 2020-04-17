from contextlib import contextmanager

import depthai
from depthai_helpers.calibration_utils import *
from depthai_helpers import utils
import argparse
from argparse import ArgumentParser
import time
import numpy as np
import os
from pathlib import Path
import shutil
import consts.resource_paths
import json

use_cv = True
try:
    import cv2
except ImportError:
    use_cv = False


def parse_args():
    epilog_text = '''
    Captures and processes images for disparity depth calibration, generating a `depthai.calib` file
    that should be loaded when initializing depthai. By default, captures one image for each of the 13 calibration target poses.

    Image capture requires the use of a printed 6x9 OpenCV checkerboard target applied to a flat surface (ex: sturdy cardboard).
    When taking photos, ensure the checkerboard fits within both the left and right image displays. The board does not need
    to fit within each drawn red polygon shape, but it should mimic the display of the polygon.

    If the calibration checkerboard corners cannot be found, the user will be prompted to try that calibration pose again.

    The script requires a RMS error < 1.0 to generate a calibration file. If RMS exceeds this threshold, an error is displayed.
    An average epipolar error of <1.5 is considered to be good, but not required. 

    Example usage:

    Run calibration with a checkerboard square size of 3.0 cm and baseline of 7.5cm:
    python3 calibrate.py -s 3.0 -b 7.5

    Only run image processing only with same board setup. Requires a set of saved capture images:
    python3 calibrate.py -s 3.0 -b 7.5 -m process
    
    Change Left/Right baseline to 15cm and swap Left/Right cameras:
    python3 calibrate.py -b 15 -w False

    Delete all existing images before starting image capture:
    python3 calibrate.py -i delete

    Pass thru pipeline config options:
    python3 calibrate.py -co '{"board_config": {"swap_left_and_right_cameras": true, "left_to_right_distance_cm": 7.5}}'
    '''
    parser = ArgumentParser(epilog=epilog_text, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-c", "--count", default=1,
                        type=int, required=False,
                        help="Number of images per polygon to capture. Default: 1.")
    parser.add_argument("-s", "--square_size_cm", default="2.5",
                        type=float, required=False,
                        help="Square size of calibration pattern used in centimeters. Default: 2.5cm.")
    parser.add_argument("-i", "--image_op", default="modify",
                        type=str, required=False,
                        help="Whether existing images should be modified or all images should be deleted before running image capture. The default is 'modify'. Change to 'delete' to delete all image files.")
    parser.add_argument("-m", "--mode", default=['capture','process'], nargs='*',
                        type=str, required=False,
                        help="Space-separated list of calibration options to run. By default, executes the full 'capture process' pipeline. To execute a single step, enter just that step (ex: 'process').")
    parser.add_argument("-co", "--config_overwrite", default=None,
                        type=str, required=False,
                        help="JSON-formatted pipeline config object. This will be override defaults used in this script.")
    parser.add_argument("-fv", "--field-of-view", default=71.86, type=float,
                        help="Horizontal field of view (HFOV) for the stereo cameras in [deg]. Default: 71.86deg.")
    parser.add_argument("-b", "--baseline", default=9.0, type=float,
                        help="Left/Right camera baseline in [cm]. Default: 9.0cm.")
    parser.add_argument("-w", "--no-swap-lr", dest="swap_lr", default=True, action="store_false",
                        help="Do not swap the Left and Right cameras. Default: True.")

    options = parser.parse_args()

    return options


def find_chessboard(frame):
    chessboard_flags = cv2.CALIB_CB_ADAPTIVE_THRESH + cv2.CALIB_CB_FAST_CHECK + cv2.CALIB_CB_NORMALIZE_IMAGE
    small_frame = cv2.resize(frame, (0, 0), fx=0.3, fy=0.3)
    return cv2.findChessboardCorners(small_frame, (9, 6), chessboard_flags)[0] and \
           cv2.findChessboardCorners(frame, (9, 6), chessboard_flags)[0]


class Main:
    output_scale_factor = 0.5
    cmd_file = consts.resource_paths.device_cmd_fpath
    polygons = None
    width = None
    height = None
    current_polygon = 0
    images_captured_polygon = 0
    images_captured = 0

    def __init__(self):
        self.args = vars(parse_args())
        self.config = {
            'streams': ['left', 'right'],
            'depth':
                {
                    'calibration_file': consts.resource_paths.calib_fpath,
                    # 'type': 'median',
                    'padding_factor': 0.3
                },
            'ai':
                {
                    'blob_file': consts.resource_paths.blob_fpath,
                    'blob_file_config': consts.resource_paths.blob_config_fpath
                },
            'board_config':
                {
                    'swap_left_and_right_cameras': self.args['swap_lr'],
                    'left_fov_deg':  self.args['field_of_view'],
                    'left_to_right_distance_cm': self.args['baseline'],
                }
        }
        if self.args['config_overwrite']:
            utils.merge(json.loads(self.args['config_overwrite']), self.config)
            print("Merged Pipeline config with overwrite", self.config)
        self.total_images = self.args['count'] * len(setPolygonCoordinates(1000, 600))  # random polygons for count
        print("Using Arguments=", self.args)

    @contextmanager
    def get_pipeline(self):
        # Possible to try and reboot?
        # The following doesn't work (have to manually hit switch on device)
        # depthai.reboot_device
        # time.sleep(1)
        if not depthai.init_device(cmd_file=self.cmd_file):
            raise RuntimeError("Unable to initialize device. Try to reset it")

        pipeline = depthai.create_pipeline(self.config)

        if pipeline is None:
            raise RuntimeError("Unable to create pipeline")

        try:
            yield pipeline
        finally:
            del pipeline

    def parse_frame(self, frame, stream_name):
        if not find_chessboard(frame):
            return False

        filename = image_filename(stream_name, self.current_polygon, self.images_captured)
        cv2.imwrite("dataset/{}/{}".format(stream_name, filename), frame)
        print("py: Saved image as: " + str(filename))
        return True

    def show_info_frame(self):
        info_frame = np.zeros((600, 1000, 3), np.uint8)
        print("Starting image capture. Press the [ESC] key to abort.")
        print("Will take {} total images, {} per each polygon.".format(self.total_images, self.args['count']))

        def show(position, text):
            cv2.putText(info_frame, text, position, cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0))

        show((25, 100), "Information about image capture:")
        show((25, 160), "Press the [ESC] key to abort.")
        show((25, 220), "Press the [spacebar] key to capture the image.")
        show((25, 300), "Polygon on the image represents the desired chessboard")
        show((25, 340), "position, that will provide best calibration score.")
        show((25, 400), "Will take {} total images, {} per each polygon.".format(self.total_images, self.args['count']))
        show((25, 550), "To continue, press [spacebar]...")

        cv2.imshow("info", info_frame)
        while True:
            key = cv2.waitKey(1)
            if key == ord(" "):
                cv2.destroyAllWindows()
                return
            elif key == 27 or key == ord("q"):  # 27 - ESC
                cv2.destroyAllWindows()
                raise SystemExit(0)

    def show_failed_capture_frame(self):
        width, height = int(self.width * self.output_scale_factor), int(self.height * self.output_scale_factor)
        info_frame = np.zeros((height, width, 3), np.uint8)
        print("py: Capture failed, unable to find chessboard! Fix position and press spacebar again")

        def show(position, text):
            cv2.putText(info_frame, text, position, cv2.FONT_HERSHEY_TRIPLEX, 0.7, (0, 255, 0))

        show((50, int(height / 2 - 40)), "Capture failed, unable to find chessboard!")
        show((60, int(height / 2 + 40)), "Fix position and press spacebar again")

        cv2.imshow("left", info_frame)
        cv2.imshow("right", info_frame)
        cv2.waitKey(2000)

    def capture_images(self):
        finished = False
        capturing = False
        captured_left = False
        captured_right = False
        tried_left = False
        tried_right = False
        with self.get_pipeline() as pipeline:
            packet_list = []
            while not finished:
                _, data_list = pipeline.get_available_nnet_and_data_packets()
                # Converting data_list into python list to get latest two data packets
                for packet in data_list:
                    packet_list.append(packet)
                
                # ESC or "q" to quit image capture
                key = cv2.waitKey(1)
                if key == 27 or key == ord("q"):
                    print("py: Calibration has been interrupted!")
                    raise SystemExit(0)

                #Spacebar to capture image pair
                if key == ord(" "):
                        capturing = True   
                
                if len(packet_list) > 1:    
                    metadata0 = packet_list[-2].getMetadata()    #Second-most recent data packet
                    metadata1 = packet_list[-1].getMetadata()    #Most recent data packet
                    ts0 = metadata0.getTimestamp()
                    ts1 = metadata1.getTimestamp()
                    
                    packet_pair = []
                    if abs(ts0-ts1) < 0.001: #pair must be at most 1 ms difference 
                        packet_pair = [packet_list[0], packet_list[1]]
                        
                        for packet in packet_pair:
                            frame = packet.getData()
                            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2RGB)

                            if self.polygons is None:
                                self.height, self.width, _ = frame.shape
                                self.polygons = setPolygonCoordinates(self.height, self.width)

                            if capturing:
                                if packet.stream_name == 'left' and not tried_left:
                                    captured_left = self.parse_frame(frame, packet.stream_name)
                                    tried_left = True
                                elif packet.stream_name == 'right' and not tried_right:
                                    captured_right = self.parse_frame(frame, packet.stream_name)
                                    tried_right = True

                            has_success = (packet.stream_name == "left" and captured_left) or \
                                          (packet.stream_name == "right" and captured_right)
                            cv2.putText(
                                frame,
                                "Polygon Position: {}. Captured {} of {} images.".format(
                                    self.current_polygon + 1, self.images_captured, self.total_images
                                ),
                                (0, 700), cv2.FONT_HERSHEY_TRIPLEX, 1.0, (255, 0, 0)
                            )
                            if self.polygons is not None:
                                cv2.polylines(
                                    frame, np.array([self.polygons[self.current_polygon]]),
                                    True, (0, 255, 0) if has_success else (0, 0, 255), 4
                                )

                            small_frame = cv2.resize(frame, (0, 0), fx=self.output_scale_factor, fy=self.output_scale_factor)
                            cv2.imshow(packet.stream_name, small_frame)

                            if captured_left and captured_right:
                                self.images_captured += 1
                                self.images_captured_polygon += 1
                                capturing = False
                                tried_left = False
                                tried_right = False
                                captured_left = False
                                captured_right = False

                            elif tried_left and tried_right:
                                self.show_failed_capture_frame()
                                capturing = False
                                tried_left = False
                                tried_right = False
                                captured_left = False
                                captured_right = False
                                break

                            if self.images_captured_polygon == self.args['count']:
                                self.images_captured_polygon = 0
                                self.current_polygon += 1

                                if self.current_polygon == len(self.polygons):
                                    finished = True
                                    cv2.destroyAllWindows()
                                    break
                        
                        packet_list = [] # resets packet_list to get at least two more packets

    def calibrate(self):
        print("Starting image processing")
        cal_data = StereoCalibration()
        try:
            cal_data.calibrate("dataset", self.args['square_size_cm'], "./resources/depthai.calib")
        except AssertionError as e:
            print("[ERROR] " + str(e))
            raise SystemExit(1)

    def run(self):
        if 'capture' in self.args['mode']:
            try:
                if self.args['image_op'] == 'delete':
                    shutil.rmtree('dataset/')
                Path("dataset/left").mkdir(parents=True, exist_ok=True)
                Path("dataset/right").mkdir(parents=True, exist_ok=True)
            except OSError:
                print("An error occurred trying to create image dataset directories!")
                raise
            self.show_info_frame()
            self.capture_images()
        if 'process' in self.args['mode']:
            self.calibrate()
        print('py: DONE.')


if __name__ == "__main__":
    Main().run()

#!/usr/bin/python3
import logging
from datetime import datetime
import numpy as np
import os
import socket
import time
from classify.trackprediction import RollingTrackPrediction
from load.clip import Clip, ClipTrackExtractor
from .motionconfig import MotionConfig
from .locationconfig import LocationConfig
from .clipsaver import ClipSaver
from .motiondetector import MotionDetector
from ml_tools import tools
from ml_tools.model import Model
from ml_tools.dataset import Preprocessor
from ml_tools.previewer import Previewer
from .leptonframe import Telemetry, LeptonFrame
from ml_tools.config import Config

SOCKET_NAME = "/var/run/lepton-frames"
VOSPI_DATA_SIZE = 160
TELEMETRY_PACKET_COUNT = 4


def get_classifier(config):

    """
    Returns a classifier object, which is created on demand.
    This means if the ClipClassifier is copied to a new process a new Classifier instance will be created.
    """
    t0 = datetime.now()
    logging.info("classifier loading")
    classifier = Model(
        train_config=config.train,
        session=tools.get_session(disable_gpu=not config.use_gpu),
    )
    classifier.load(config.classify.model)
    logging.info("classifier loaded ({})".format(datetime.now() - t0))

    return classifier


def main():
    try:
        os.unlink(SOCKET_NAME)
    except OSError:
        if os.path.exists(SOCKET_NAME):
            raise
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    sock.bind(SOCKET_NAME)
    sock.listen(1)
    config = Config.load_from_file()
    motion_config = MotionConfig.load_from_file()
    location_config = LocationConfig.load_from_file()
    classifier = get_classifier(config)

    clip_classifier = PiClassifier(config, motion_config, location_config, classifier)
    while True:
        print("waiting for a connection")
        connection, client_address = sock.accept()
        print("connection from", client_address)
        try:
            handle_connection(connection, clip_classifier)
        finally:
            # Clean up the connection
            connection.close()


def handle_connection(connection, clip_classifier):
    img_dtype = np.dtype("uint16")
    # big endian > little endian < from lepton3 is big endian while python is little endian
    img_dtype = img_dtype.newbyteorder(">")

    thermal_frame = np.empty(
        (clip_classifier.res_y, clip_classifier.res_x), dtype=img_dtype
    )

    while True:
        data = connection.recv(400 * 400 * 2 + TELEMETRY_PACKET_COUNT * VOSPI_DATA_SIZE)

        if not data:
            print("disconnected from camera")
            return
        else:
            telemetry = Telemetry.parse_telemetry(
                data[: TELEMETRY_PACKET_COUNT * VOSPI_DATA_SIZE]
            )

            thermal_frame = np.frombuffer(
                data, dtype=img_dtype, offset=TELEMETRY_PACKET_COUNT * VOSPI_DATA_SIZE
            ).reshape(clip_classifier.res_y, clip_classifier.res_x)
            if len(thermal_frame[thermal_frame > 10000]):
                print(
                    "data is {}, thermal frame max {} thermal frame min {} telemetry time_on {}".format(
                        len(data),
                        np.amax(thermal_frame),
                        np.amin(thermal_frame),
                        telemetry.time_on / 1000.0,
                    )
                )
            lepton_frame = LeptonFrame(telemetry, thermal_frame)
            clip_classifier.process_frame(lepton_frame)


class PiClassifier:
    """ Classifies tracks within CPTV files. """

    PROCESS_FRAME = 1

    def __init__(self, config, motion_config, location_config, classifier):
        """ Create an instance of a clip classifier"""
        # prediction record for each track
        self.frame_num = 0

        self.tracking = False
        self.config = config
        self.track_prediction = {}
        self.classifier = classifier
        self.previewer = Previewer.create_if_required(config, config.classify.preview)
        self.num_labels = len(classifier.labels)
        self.res_x = self.config.classify.res_x
        self.res_y = self.config.classify.res_y
        self.cache_to_disk = config.classify.cache_to_disk
        # enables exports detailed information for each track.  If preview mode is enabled also enables track previews.
        self.enable_per_track_information = False
        self.prediction_per_track = {}
        self.preview_frames = motion_config.preview_secs * motion_config.frame_rate
        edge = self.config.tracking.edge_pixels
        self.crop_rectangle = tools.Rectangle(
            edge, edge, self.res_x - 2 * edge, self.res_y - 2 * edge
        )

        try:
            self.fp_index = self.classifier.labels.index("false-positive")
        except ValueError:
            self.fp_index = None

        self.track_extractor = ClipTrackExtractor(
            self.config.tracking,
            self.config.use_opt_flow,
            self.config.classify.cache_to_disk,
            keep_frames=True,
            calc_stats=False,
        )
        self.motion_config = motion_config
        self.min_frames = self.motion_config.min_secs * self.preview_frames
        self.max_frames = self.motion_config.max_secs * self.preview_frames
        self.motion_detector = MotionDetector(
            self.res_x,
            self.res_y,
            motion_config,
            location_config,
            self.config.tracking.dynamic_thresh,
        )

        self.clip_saver = ClipSaver("piclips")
        self.startup_classifier()

    def new_clip(self):
        self.clip = Clip(self.config.tracking, "stream")
        self.clip.video_start_time = datetime.now()
        self.clip.num_preview_frames = self.preview_frames
        self.clip.set_res(self.res_x, self.res_y)
        self.clip.set_frame_buffer(
            self.config.classify_tracking.high_quality_optical_flow,
            self.cache_to_disk,
            self.config.use_opt_flow,
            True,
        )

        # process preview_frames
        cur_frame = self.motion_detector.thermal_window.oldest_index
        num_frames = 0
        while num_frames < self.preview_frames:
            frame = self.motion_detector.thermal_window.get(cur_frame)
            self.track_extractor.process_frame(self.clip, frame)
            cur_frame += 1
            num_frames += 1

    def startup_classifier(self):
        p_frame = np.zeros((5, 48, 48), np.float32)
        self.classifier.classify_frame_with_novelty(p_frame, None)

    def identify_last_frame(self):
        """
        Runs through track identifying segments, and then returns it's prediction of what kind of animal this is.
        One prediction will be made for every frame.
        :param track: the track to identify.
        :return: TrackPrediction object
        """

        prediction_smooth = 0.1

        smooth_prediction = None
        smooth_novelty = None

        prediction = 0.0
        novelty = 0.0

        active_tracks = self.clip.active_tracks
        frame = self.clip.frame_buffer.get_last_frame()
        if frame is None:
            return
        thermal_reference = np.median(frame.thermal)

        for i, track in enumerate(active_tracks):
            track_prediction = self.prediction_per_track.setdefault(
                track.get_id(), RollingTrackPrediction(track.get_id())
            )
            region = track.bounds_history[-1]
            if region.frame_number != frame.frame_number:
                print("frame doesn't match last frame")
            else:
                track_data = track.crop_by_region(frame, region)
                # we use a tigher cropping here so we disable the default 2 pixel inset
                frames = Preprocessor.apply(
                    [track_data], [thermal_reference], default_inset=0
                )
                if frames is None:
                    logging.info(
                        "Frame {} of track could not be classified.".format(
                            region.frame_number
                        )
                    )
                    return

                p_frame = frames[0]
                prediction, novelty, state = self.classifier.classify_frame_with_novelty(
                    p_frame, track_prediction.state
                )
                track_prediction.state = state

                if self.fp_index is not None:
                    prediction[self.fp_index] *= 0.8
                state *= 0.98
                mass = region.mass
                mass_weight = np.clip(mass / 20, 0.02, 1.0) ** 0.5
                cropped_weight = 0.7 if region.was_cropped else 1.0

                prediction *= mass_weight * cropped_weight

                if smooth_prediction is None:
                    if track_prediction.uniform_prior:
                        smooth_prediction = np.ones([self.num_labels]) * (
                            1 / self.num_labels
                        )
                    else:
                        smooth_prediction = prediction
                    smooth_novelty = 0.5
                else:
                    smooth_prediction = (
                        1 - prediction_smooth
                    ) * smooth_prediction + prediction_smooth * prediction
                    smooth_novelty = (
                        1 - prediction_smooth
                    ) * smooth_novelty + prediction_smooth * novelty

                track_prediction.predictions.append(smooth_prediction)
                track_prediction.novelties.append(smooth_novelty)

    def get_clip_prediction(self):
        """ Returns list of class predictions for all tracks in this clip. """

        class_best_score = [0 for _ in range(len(self.classifier.labels))]

        # keep track of our highest confidence over every track for each class
        for _, prediction in self.track_prediction.items():
            for i in range(len(self.classifier.labels)):
                class_best_score[i] = max(
                    class_best_score[i], prediction.class_best_score[i]
                )
        self.clip.tracks = []
        results = []
        for n in range(1, 1 + len(self.classifier.labels)):
            nth_label = int(np.argsort(class_best_score)[-n])
            nth_score = float(np.sort(class_best_score)[-n])
            results.append((self.classifier.labels[nth_label], nth_score))

        return results

    def process_frame(self, lepton_frame):
        """
        Process a file extracting tracks and identifying them.
        :param filename: filename to process
        :param enable_preview: if true an MPEG preview file is created.
        """
        self.motion_detector.process_frame(lepton_frame)
        if self.tracking is False:
            if self.motion_detector.movement_detected:
                self.tracking = True
                self.motion_detector.start_recording()
                self.new_clip()
        else:
            if self.clip.frame_on > self.min_frames:
                self.tracking = self.motion_detector.movement_detected

            if self.tracking:
                self.track_extractor.process_frame(self.clip, lepton_frame.pix)
                if len(lepton_frame.pix[lepton_frame.pix > 10000]):
                    print(
                        "thermal frame max {} thermal frame min {} frame {}".format(
                            np.amax(lepton_frame.pix),
                            np.amin(lepton_frame.pix),
                            self.clip.frame_on,
                        )
                    )
                if self.clip.active_tracks and (
                    self.clip.frame_on % PiClassifier.PROCESS_FRAME == 0
                    or self.clip.frame_on == self.preview_frames
                ):
                    # self.identify_last_frame()
                    # for track in self.clip.active_tracks:
                    #     prediction = self.prediction_per_track[track.get_id()]
                    #     print(
                    #         "Track {} is {}".format(
                    #             track.get_id(),
                    #             prediction.get_classified_footer(
                    #                 self.classifier.labels
                    #             ),
                    #         )
                    #     )
            elif self.tracking is False or self.clip.frame_on == self.max_frames:
                # finish recording
                print("finishing recording")
                for track in self.clip.tracks:
                    track_prediction = self.prediction_per_track.get(
                        track.get_id(), None
                    )
                    if track_prediction:
                        track_result = track_prediction.get_result(
                            self.classifier.labels
                        )
                        track.confidence = track_result.confidence
                        track.tag = track_result.what
                        track.max_novelty = track_result.max_novelty
                        track.avg_novelty = track_result.avg_novelty
                self.write_clip()
                self.prediction_per_track = {}
                self.clip = None
                self.tracking = False
                self.motion_detector.stop_recording()

        self.frame_num += 1

    def write_clip(self):
        # write clip to h5py for now
        self.clip_saver.add_clip(self.clip)

    def tracking_to_jpg(self, frame):
        h_min = np.amin(frame.thermal)
        h_max = np.amax(frame.thermal)
        four_stacked = Previewer.create_four_tracking_image(frame, h_min)

        image = self.previewer.convert_and_resize(four_stacked, h_min, h_max, 3.0)
        self.previewer.add_last_frame_tracking(
            image, self.clip.active_tracks, self.classifier.labels
        )

        filename = "test-{}-{}".format(time.time(), self.frame_num)
        image.save(filename + ".jpg", "JPEG")

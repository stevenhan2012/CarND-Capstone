#!/usr/bin/env python
import rospy
from std_msgs.msg import Int32
from geometry_msgs.msg import PoseStamped, Pose
from styx_msgs.msg import TrafficLightArray, TrafficLight
from styx_msgs.msg import Lane, Waypoint
from sensor_msgs.msg import Image
from cv_bridge import CvBridge
from light_classification.tl_classifier import TLClassifier
import tf
import yaml
import sys
import math
from scipy.spatial import KDTree


STATE_COUNT_THRESHOLD = 3
UNKNOWN = -1
MAX_DISTANCE = sys.maxint


class TLDetector(object):
    def __init__(self):
        rospy.init_node('tl_detector')

        self.pose = None
        self.waypoints = None
        self.camera_image = None
        self.lights = []
        self.waypoints_2d = None
        self.waypoint_tree = None

        sub1 = rospy.Subscriber('/current_pose', PoseStamped, self.pose_cb)
        sub2 = rospy.Subscriber('/base_waypoints', Lane, self.waypoints_cb)

        '''
        /vehicle/traffic_lights provides you with the location of the traffic light in 3D map space and
        helps you acquire an accurate ground truth data source for the traffic light
        classifier by sending the current color state of all traffic lights in the
        simulator. When testing on the vehicle, the color state will not be available. You'll need to
        rely on the position of the light and the camera image to predict it.
        '''
        sub3 = rospy.Subscriber('/vehicle/traffic_lights', TrafficLightArray, self.traffic_cb)
        sub6 = rospy.Subscriber('/image_color', Image, self.image_cb)

        config_string = rospy.get_param("/traffic_light_config")
        self.config = yaml.load(config_string)
        self.stop_line_positions = []
        self.get_stop_line_positions()

        self.upcoming_red_light_pub = rospy.Publisher('/traffic_waypoint', Int32, queue_size=1)

        self.bridge = CvBridge()
        self.light_classifier = TLClassifier()
        self.listener = tf.TransformListener()

        self.state = TrafficLight.UNKNOWN
        self.last_state = TrafficLight.UNKNOWN
        self.last_wp = -1
        self.state_count = 0

        rospy.spin()

    def pose_cb(self, msg):
        self.pose = msg

    def waypoints_cb(self, waypoints):
        if not self.waypoints_2d:
            self.waypoints_2d = [[waypoint.pose.pose.position.x, waypoint.pose.pose.position.y] for waypoint in waypoints.waypoints]
            self.waypoint_tree = KDTree(self.waypoints_2d)
        self.waypoints = waypoints

    def traffic_cb(self, msg):
        self.lights = msg.lights

    def image_cb(self, msg):
        """Identifies red lights in the incoming camera image and publishes the index
            of the waypoint closest to the red light's stop line to /traffic_waypoint

        Args:
            msg (Image): image from car-mounted camera

        """

        self.has_image = True
        self.camera_image = msg
        light_wp, state = self.process_traffic_lights()

        '''
        Publish upcoming red lights at camera frequency.
        Each predicted state has to occur `STATE_COUNT_THRESHOLD` number
        of times till we start using it. Otherwise the previous stable state is
        used.
        '''
        if self.state != state:
            self.state_count = 0
            self.state = state
        elif self.state_count >= STATE_COUNT_THRESHOLD:
            self.last_state = self.state
            light_wp = light_wp if state == TrafficLight.RED else -1
            self.last_wp = light_wp
            self.upcoming_red_light_pub.publish(Int32(light_wp))
        else:
            self.upcoming_red_light_pub.publish(Int32(self.last_wp))
        self.state_count += 1

    def get_closest_waypoint(self, x, y):
        """Identifies the closest path waypoint to the given position
            https://en.wikipedia.org/wiki/Closest_pair_of_points_problem
        Args:
            pose (Pose): position to match a waypoint to

        Returns:
            int: index of the closest waypoint in self.waypoints

        """
        closest_idx = self.waypoint_tree.query([x, y], 1)[1]
        return closest_idx

    def get_light_state(self, light):
        """Determines the current color of the traffic light

        Args:
            light (TrafficLight): light to classify

        Returns:
            int: ID of traffic light color (specified in styx_msgs/TrafficLight)

        """

        if not self.has_image:
            self.prev_light_loc = None
            return False

        cv_image = self.bridge.imgmsg_to_cv2(self.camera_image, "bgr8")

        # Get classification
        predicted = self.light_classifier.get_classification(cv_image)

        # rospy.loginfo("traffic light state: %d", light.state)

        return predicted

    def process_traffic_lights(self):
        """Finds closest visible traffic light, if one exists, and determines its
            location and color

        Returns:
            int: index of waypoint closes to the upcoming stop line for a traffic light (-1 if none exists)
            int: ID of traffic light color (specified in styx_msgs/TrafficLight)

        """

        light = None
        distance_tolerance = 300
        stop_line_position = None

        # List of positions that correspond to the line to stop in front of for a given intersection

        if self.pose and self.waypoints:
            car_index = self.get_closest_waypoint(self.pose.pose.position.x, self.pose.pose.position.y)
            car_position = self.waypoints.waypoints[car_index].pose.pose.position
            # rospy.loginfo("Car Position:{}".format(car_position))

            light_index = self.get_closest_light(car_position)
            if light_index != UNKNOWN:
                light_waypoint_index = self.get_closest_waypoint(self.lights[light_index].pose.pose.position.x,
                                                                 self.lights[light_index].pose.pose.position.y)
                light_position = self.waypoints.waypoints[light_waypoint_index].pose.pose.position
                # rospy.loginfo("Nearest Light Position:{}".format(light_position))

                if light_waypoint_index > car_index:
                    distance_to_traffic_light = self.distance_of_positions(car_position, light_position)
                    if distance_to_traffic_light < distance_tolerance:
                        light = self.lights[light_index]
                        stop_line_index = self.get_closest_stop_line(light_position)
                        stop_line_position = self.stop_line_positions[stop_line_index].pose.pose
                        stop_line_waypoint = self.get_closest_waypoint(stop_line_position.position.x,
                                                                       stop_line_position.position.y)
                        # rospy.loginfo("Nearest Stop Line within Range:{}".format(stop_line_position))

        if light and stop_line_position:
            # if int(distance_to_traffic_light) % 5 == 0:
            #    rospy.logwarn("### Traffic Light Detected in: %.0f m", distance_to_traffic_light)
            state = self.get_light_state(light)
            return stop_line_waypoint, state

        return -1, TrafficLight.UNKNOWN

    def get_closest_light(self, pose):
        return self.get_closest_index(pose, self.lights)

    def get_closest_index(self, pose, positions):
        minimal_distance = MAX_DISTANCE
        index = UNKNOWN

        for i in range(len(positions)):
            distance = self.distance_of_positions(pose, positions[i].pose.pose.position)
            if distance < minimal_distance:
                minimal_distance = distance
                index = i

        return index

    def get_closest_stop_line(self, pose):
        return self.get_closest_index(pose, self.stop_line_positions)

    def distance_of_positions(self, pos1, pos2):
        return math.sqrt((pos1.x - pos2.x) ** 2 + (pos1.y - pos2.y) ** 2 + (pos1.z - pos2.z) ** 2)

    def get_stop_line_positions(self):
        for light_position in self.config['stop_line_positions']:
            p = Waypoint()
            p.pose.pose.position.x = light_position[0]
            p.pose.pose.position.y = light_position[1]
            p.pose.pose.position.z = 0.0
            self.stop_line_positions.append(p)


if __name__ == '__main__':
    try:
        TLDetector()
    except rospy.ROSInterruptException:
        rospy.logerr('Could not start traffic node.')

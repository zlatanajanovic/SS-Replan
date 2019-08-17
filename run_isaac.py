#!/usr/bin/env python2

from __future__ import print_function

import sys
import os
import rospy
import traceback
import numpy as np
import math

sys.path.extend(os.path.abspath(os.path.join(os.getcwd(), d))
                for d in ['pddlstream', 'ss-pybullet'])

from brain_ros.kitchen_domain import KitchenDomain
#from brain_ros.demo_kitchen_domain import KitchenDomain as DemoKitchenDomain
#from grasps import *
from brain_ros.sim_test_tools import TrialManager
from brain_ros.ros_world_state import RosObserver
from isaac_bridge.carter import Carter

from pybullet_tools.utils import LockRenderer, wait_for_user, unit_from_theta
from pddlstream.utils import Verbose

from src.policy import run_policy
from src.interface import Interface
from src.command import execute_commands, iterate_commands
from src.parse_brain import task_from_trial_manager, create_trial_args, TASKS, SPAM, MUSTARD, TOMATO_SOUP, SUGAR, \
    CHEEZIT, YCB_OBJECTS, ECHO_COUNTER, INDIGO_COUNTER, TOP_DRAWER
from src.utils import JOINT_TEMPLATE
from src.visualization import add_markers
from src.issac import observe_world, kill_lula, update_isaac_sim, update_robot_conf
from src.world import World
from run_pybullet import create_parser
from src.planner import simulate_plan
from src.task import Task, CRACKER_POSE2D, SPAM_POSE2D, pose2d_on_surface, sample_placement
from examples.discrete_belief.dist import DDist, UniformDist, DeltaDist
#from src.issac import display_kinect
from src.execution import franka_open_gripper

def planning_loop(interface):
    args = interface.args

    def observation_fn():
        interface.localize_all()
        return observe_world(interface)

    def transition_fn(belief, commands):
        sim_state = belief.sample_state()
        if args.watch or args.record:
            wait_for_user()
            # simulate_plan(sim_state.copy(), commands, args)
            iterate_commands(sim_state.copy(), commands)
            wait_for_user()
        sim_state.assign()
        if args.teleport or args.cfree:
            print('Some constraints were ignored. Skipping execution!')
            return False
        # TODO: could calibrate closed-loop relative to the object
        # Terminate if failed to pick up
        success = execute_commands(interface, commands)
        update_robot_conf(interface)
        return success

    return run_policy(interface.task, args, observation_fn, transition_fn)

################################################################################

def test_carter(interface):
    carter = interface.carter

    assert carter is not None
    carter_pose = carter.current_pose
    print('Carter pose:', carter_pose)
    x, y, theta = carter_pose  # current_velocity
    pos = np.array([x, y])
    goal_pos = pos + 1.0 * unit_from_theta(theta)
    goal_pose = np.append(goal_pos, [theta])
    #goal_pose = np.append(pos, [0.])

    # carter.move_to(goal_pose) # recursion bug
    carter.move_to_safe(goal_pose)  # move_to_async | move_to_safe
    #carter.move_to_openloop(goal_pose)
    # move_to_open_loop | move_to_safe_followed_by_openloop

    carter.simple_move(-0.1) # simple_move | simple_stop
    # rospy.sleep(2.0)
    # carter.simple_stop()
    #domain.get_robot().carter_interface = interface.carter
    # domain.get_robot().unsuppress_fixed_bases()

    # /sim/tf to get all objects
    # https://gitlab-master.nvidia.com/srl/srl_system/blob/722d127a016c9105ec68a33902a73480c36b31ac/packages/isaac_bridge/scripts/sim_tf_relay.py
    # sim_tf_relay.py

    # roslaunch isaac_bridge sim_franka.launch cooked_sim:=true config:=panda_full lula:=false world:=franka_leftright_kitchen_ycb_world.yaml
    # https://gitlab-master.nvidia.com/srl/srl_system/blob/fb94253c60b1bd1308a37c1aeb9dc4a4c453c512/packages/isaac_bridge/launch/sim_franka.launch
    # packages/external/lula_franka/config/worlds/franka_center_right_kitchen.sim.yaml
    # packages/external/lula_franka/config/worlds/franka_center_right_kitchen.yaml

#   File "/home/cpaxton/srl_system/workspace/src/brain/src/brain_ros/ros_world_state.py", line 397, in update_msg
#     self.gripper = msg.get_positions([self.gripper_joint])[0]
# TypeError: 'NoneType' object has no attribute '__getitem__'

def set_isaac_sim(interface):
    assert interface.simulation
    task = interface.task
    world = task.world
    task = world.task
    # close_all_doors(world)
    if task.movable_base:
        world.set_base_conf([2.0, 0, -np.pi / 2])
        # world.set_initial_conf()
    else:
        for name, dist in task.prior.items():
            surface = dist.sample()
            sample_placement(world, name, surface, learned=False)
        # pose2d_on_surface(world, SPAM, INDIGO_COUNTER, pose2d=SPAM_POSE2D)
        # pose2d_on_surface(world, CHEEZIT, INDIGO_COUNTER, pose2d=CRACKER_POSE2D)
    update_isaac_sim(interface, world)
    # wait_for_user()

################################################################################

def simulation_setup(domain, world, args):
    # TODO: forcibly reset robot configuration
    # trial_args = parse.parse_kitchen_args()
    trial_args = create_trial_args()
    with Verbose(False):
        trial_manager = TrialManager(trial_args, domain, lula=args.lula)
    observer = trial_manager.observer
    task_name = args.problem.replace('_', ' ')
    task = task_from_trial_manager(world, trial_manager, task_name, fixed=args.fixed)
    interface = Interface(args, task, observer, trial_manager=trial_manager)
    if args.jump:
        robot_entity = domain.get_robot()
        robot_entity.carter_interface = interface.sim_manager
    return interface


def real_setup(domain, world, args):
    # TODO: detect if lula is active
    observer = RosObserver(domain)
    prior = {
        SPAM: DeltaDist(INDIGO_COUNTER),
        SUGAR: DeltaDist(INDIGO_COUNTER),
        CHEEZIT: DeltaDist(INDIGO_COUNTER),
    }
    task = Task(world, prior=prior,
                # goal_holding=[SPAM],
                goal_on={SPAM: TOP_DRAWER},
                #goal_closed=[],
                goal_closed=[JOINT_TEMPLATE.format(TOP_DRAWER)],  # , 'indigo_drawer_bottom_joint'],
                #goal_open=[JOINT_TEMPLATE.format(TOP_DRAWER)],
                movable_base=not args.fixed,
                return_init_bq=True, return_init_aq=True)

    if not args.fixed:
        carter = Carter(goal_threshold_tra=0.10,
                        goal_threshold_rot=math.radians(15.),
                        vel_threshold_lin=0.01,
                        vel_threshold_ang=math.radians(1.0))
        robot_entity = domain.get_robot()
        robot_entity.carter_interface = carter
        robot_entity.unsuppress_fixed_bases()
    return Interface(args, task, observer)


################################################################################

def main():
    parser = create_parser()
    parser.add_argument('-execute', action='store_true',
                        help="When enabled, uses the real robot_entity")
    parser.add_argument('-fixed', action='store_true',
                        help="When enabled, fixes the robot_entity's base")
    parser.add_argument('-jump', action='store_true',
                        help="When enabled, skips base control")
    parser.add_argument('-lula', action='store_true',
                        help='When enabled, uses LULA instead of JointState control')
    parser.add_argument('-problem', default=TASKS[2], choices=TASKS,
                        help='The name of the task')
    parser.add_argument('-watch', action='store_true',
                        help='When enabled, plans are visualized in PyBullet before executing in IsaacSim')
    args = parser.parse_args()
    np.set_printoptions(precision=3, suppress=True)
    #args.watch |= args.execute
    # TODO: samples from the belief distribution likely don't have the init flag

    # TODO: populate with initial objects even if not observed
    # TODO: reobserve thee same scene until receive good observation
    # TODO: integrate with deepim

    # srl_system/packages/isaac_bridge/configs/ycb_table_config.json
    # srl_system/packages/isaac_bridge/configs/ycb_table_graph.json
    # srl_system/packages/isaac_bridge/configs/panda_full_config.json
    # srl_system/packages/isaac_bridge/configs/panda_full_graph.json
    # alice/assets/maps/seattle_map_res02_181214.config.json

    # https://gitlab-master.nvidia.com/srl/srl_system/blob/c5747181a24319ed1905029df6ebf49b54f1c803/packages/brain/src/brain_ros/lula_policies.py#L464
    rospy.init_node("STRIPStream")
    #with HideOutput():
    #if args.execute:
    #    domain = DemoKitchenDomain(sim=not args.execute, use_carter=True) # TODO: broken
    #else:
    with Verbose(False):
        domain = KitchenDomain(sim=not args.execute, sigma=0, lula=args.lula)
    robot_entity = domain.get_robot()
    robot_entity.get_motion_interface().remove_obstacle()

    robot_entity.suppress_fixed_bases() # Not as much error?
    #robot_entity.unsuppress_fixed_bases() # Significant error
    # Significant error without either
    #print(dump_dict(robot_entity))

    # /home/cpaxton/srl_system/workspace/src/external/lula_franka
    world = World(use_gui=True) # args.visualize)
    if args.execute:
        interface = real_setup(domain, world, args)
    else:
        interface = simulation_setup(domain, world, args)
    franka_open_gripper(interface)
    # display_kinect(interface, side='left')
    #test_carter(interface)
    #return

    # Can disable lula world objects to improve speed
    # Adjust DART to get a better estimate for the drawer joints
    #localize_all(world_state)
    #wait_for_user()
    #print('Entities:', sorted(world_state.entities))
    with LockRenderer(lock=True):
        # Used to need to do expensive computation before localize_all
        # due to the LULA overhead (e.g. loading complex meshes)
        observe_world(interface)
        if interface.simulation:  # TODO: move to simulation instead?
            set_isaac_sim(interface)
        world._update_initial()
        add_markers(world, inverse_place=False)

    #base_control(world, [2.0, 0, -3*np.pi / 4], domain.get_robot().get_motion_interface(), observer)
    #return

    success = planning_loop(interface)
    print('Success:', success)
    world.destroy()

# cpaxton@lokeefe:~/alice$ bazel run apps/samples/navigation_rosbridge
# srl@carter:~/deploy/srl/carter-pkg$ ./apps/carter/carter -r 2 -m seattle_map_res02_181214
# cpaxton@lokeefe:~$ roscore
# cpaxton@lokeefe:~/srl_system/workspace/src/brain/src/brain_ros$ rosrun lula_dart object_administrator --detect --j=00_potted_meat_can
# cpaxton@lokeefe:~$ franka world franka_center_right_kitchen.yaml

################################################################################

if __name__ == '__main__':
    #main()
    try:
        main()
    except: # BaseException as e:
        traceback.print_exc()
        #raise e
    finally:
        kill_lula()

# 3 real robot control options:
# 1) LULA + RMP
# 2) Position joint trajectory controller
# 3) LULA backend directly

# Running in IsaacSim
# 1) roslaunch isaac_bridge sim_franka.launch cooked_sim:=true config:=panda_full lula:=false

# Running on the real robot w/o LULA
# 1) roslaunch franka_controllers start_control.launch
# 2) roslaunch panda_moveit_config panda_control_moveit_rviz.launch load_gripper:=True robot_ip:=172.16.0.2
# 3) srl@vgilligan:~/srl_system/workspace/src/brain$ ./relay.sh
# 3) cpaxton@lokeefe:~/srl_system/workspace/src/external/lula_franka$ franka viz
# 4) killall move_group franka_control_node local_controller

# Running on the real robot w/ lula
# 1) franka_backend
# 2) roslaunch panda_moveit_config start_moveit.launch
# 3) ...

# Adjusting impedance thresholds to allow contact
# /franka_control/set_cartesian_impedance
# /franka_control/set_force_torque_collision_behavior
# /franka_control/set_full_collision_behavior
# /franka_control/set_joint_impedance
# srl@vgilligan:~/srl_system/workspace/src/third_party/franka_controllers/scripts
# rosed franka_controllers set_parameters

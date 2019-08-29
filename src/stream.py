import numpy as np
import random
from itertools import islice, cycle
from collections import namedtuple

from pybullet_tools.pr2_utils import is_visible_point, get_view_aabb, support_from_aabb, get_top_presses
from pybullet_tools.utils import pairwise_collision, multiply, invert, get_joint_positions, BodySaver, get_distance, \
    set_joint_positions, plan_direct_joint_motion, plan_joint_motion, \
    get_custom_limits, all_between, uniform_pose_generator, plan_nonholonomic_motion, link_from_name, get_extend_fn, \
    joint_from_name, get_link_subtree, get_link_name, get_link_pose, \
    Euler, quat_from_euler, set_pose, point_from_pose, sample_placement_on_aabb, get_sample_fn, get_pose, \
    stable_z_on_aabb, euler_from_quat, quat_from_pose, wrap_angle, wait_for_user, \
    Ray, get_distance_fn, get_unit_vector, unit_quat, Point, set_configuration, \
    is_point_in_polygon, grow_polygon, Pose, get_moving_links, get_aabb_extent, get_aabb_center, \
    set_renderer, get_movable_joints, INF, apply_affine, get_joint_name, unit_point, \
    get_aabb, draw_aabb, remove_handles
from pddlstream.algorithms.downward import MAX_FD_COST #, get_cost_scale

from src.command import Sequence, Trajectory, ApproachTrajectory, Attach, Detach, State, DoorTrajectory, \
    Detect, AttachGripper
from src.database import load_placements, get_surface_reference_pose, load_place_base_poses, \
    load_pull_base_poses, load_forward_placements, load_inverse_placements
from src.utils import get_grasps, iterate_approach_path, APPROACH_DISTANCE, ALL_SURFACES, \
    set_tool_pose, close_until_collision, get_descendant_obstacles, surface_from_name, RelPose, FINGER_EXTENT, create_surface_attachment, \
    compute_surface_aabb, create_relative_pose, Z_EPSILON, get_surface_obstacles, test_supported, \
    get_link_obstacles, ENV_SURFACES, FConf, open_surface_joints, DRAWERS, STOVE_LOCATIONS, STOVES, \
    TOOL_POSE, Grasp, TOP_GRASP, KNOBS, JOINT_TEMPLATE
from src.visualization import GROW_INVERSE_BASE, GROW_FORWARD_RADIUS
from src.inference import SurfaceDist, NUM_PARTICLES
from examples.discrete_belief.run import revisit_mdp_cost, clip_cost, DDist #, MAX_COST

COST_SCALE = 1e3 # 3 decimal places
MAX_COST = MAX_FD_COST / COST_SCALE
#MAX_COST = MAX_FD_COST / get_cost_scale()
# TODO: move this to FD

DETECT_COST = 1.0
BASE_CONSTANT = 1.0 # 1 | 10
BASE_VELOCITY = 0.25
SELF_COLLISIONS = True

PAUSE_MOTION_FAILURES = False
PRINT_FAILURES = True
MOVE_ARM = True
ARM_RESOLUTION = 0.05
GRIPPER_RESOLUTION = 0.01
DOOR_RESOLUTION = 0.025

# TracIK is itself stochastic
P_RANDOMIZE_IK = 0.25  # 0.0 | 0.5

MAX_CONF_DISTANCE = 0.75
NEARBY_APPROACH = MAX_CONF_DISTANCE
NEARBY_PULL = 0.25

# TODO: TracIK might not be deterministic in which case it might make sense to try a few
# http://docs.ros.org/kinetic/api/moveit_tutorials/html/doc/trac_ik/trac_ik_tutorial.html
# http://wiki.ros.org/trac_ik
# https://traclabs.com/projects/trac-ik/
# https://bitbucket.org/traclabs/trac_ik/src/master/
# https://bitbucket.org/traclabs/trac_ik/src/master/trac_ik_lib/
# Speed: returns very quickly the first solution found
# Distance: runs for the full timeout_in_secs, then returns the solution that minimizes SSE from the seed
# Manip1: runs for full timeout, returns solution that maximizes sqrt(det(J*J^T))
# Manip2: runs for full timeout, returns solution that minimizes cond(J) = |J|*|J^-1|

# ik_solver.set_joint_limits([0.0]* ik_solver.number_of_joints, upper_bound)

# TODO: need to wrap trajectory when executing in simulation or running on the robot

################################################################################

def base_cost_fn(q1, q2):
    distance = get_distance(q1.values[:2], q2.values[:2])
    return BASE_CONSTANT + distance / BASE_VELOCITY

def trajectory_cost_fn(t):
    distance = t.distance(distance_fn=lambda q1, q2: get_distance(q1[:2], q2[:2]))
    return BASE_CONSTANT + distance / BASE_VELOCITY

def compute_detect_cost(prob):
    success_cost = DETECT_COST
    failure_cost = success_cost
    cost = revisit_mdp_cost(success_cost, failure_cost, prob)
    return cost

def detect_cost_fn(obj_name, rp_dist, obs, rp_sample):
    # TODO: extend to continuous rp_sample controls using densities
    # TODO: count samples in a nearby vicinity to be invariant to number of samples
    prob = rp_dist.discrete_prob(rp_sample)
    cost = clip_cost(compute_detect_cost(prob), max_cost=MAX_COST)
    #print('{}) Detect Prob: {:.3f} | Detect Cost: {:.3f}'.format(
    #    rp_dist.surface_name, prob, cost))
    return cost

################################################################################

# TODO: more general forward kinematics

def get_compute_pose_kin(world):
    #obstacles = world.static_obstacles

    def fn(o1, rp, o2, p2):
        if o1 == o2:
            return None
        if isinstance(rp, SurfaceDist):
            p1 = rp.project(lambda x: fn(o1, x, o2, p2)[0]) # TODO: filter if any in collision
            return (p1,)
        #if np.allclose(p2.value, unit_pose()):
        #    return (rp,)
        #if np.allclose(rp.value, unit_pose()):
        #    return (p2,)
        # TODO: assert that the links align?
        body = world.get_body(o1)
        p1 = RelPose(body, #reference_body=p2.reference_body, reference_link=p2.reference_link,
                     support=rp.support, confs=(p2.confs + rp.confs),
                     init=(rp.init and p2.init))
        #p1.assign()
        #if any(pairwise_collision(body, obst) for obst in obstacles):
        #    return None
        return (p1,)
    return fn

def get_compute_angle_kin(world):
    def fn(o, j, a):
        link = link_from_name(world.kitchen, o) # link not surface
        p = RelPose(world.kitchen, # link,
                    confs=[a], init=a.init)
        return (p,)
    return fn

################################################################################

def get_compute_detect(world, ray_trace=True, **kwargs):
    obstacles = world.static_obstacles
    scale = 0.05 # 0.5

    def fn(obj_name, pose):
        # TODO: incorporate probability mass
        # Ether sample observation (control) or target belief (next state)
        body = world.get_body(obj_name)
        open_surface_joints(world, pose.support)
        for camera_name in world.cameras:
            camera_body, camera_matrix, camera_depth = world.cameras[camera_name]
            camera_pose = get_pose(camera_body)
            camera_point = point_from_pose(camera_pose)
            obj_point = point_from_pose(pose.get_world_from_body())

            aabb = get_view_aabb(body, camera_pose)
            center = get_aabb_center(aabb)
            extent = np.multiply([scale, scale, 1], get_aabb_extent(aabb))
            view_aabb = (center - extent / 2, center + extent / 2)
            # print(is_visible_aabb(view_aabb, camera_matrix=camera_matrix))
            obj_points = apply_affine(camera_pose, support_from_aabb(view_aabb)) + [obj_point]
            # obj_points = [obj_point]
            if not all(is_visible_point(camera_matrix, camera_depth, point, camera_pose)
                       for point in obj_points):
                continue
            rays = [Ray(camera_point, point) for point in obj_points]
            detect = Detect(world, camera_name, obj_name, pose, rays)
            if ray_trace:
                # TODO: how should doors be handled?
                move_occluding(world)
                open_surface_joints(world, pose.support)
                detect.pose.assign()
                if obstacles & detect.compute_occluding():
                    continue
            #detect.draw()
            #wait_for_user()
            return (detect,)
        return None
    return fn


def move_occluding(world):
    # Prevent obstruction by other objects
    # TODO: this is a bit of a hack due to pybullet
    world.set_base_conf([-5.0, 0, 0])
    for joint in world.kitchen_joints:
        joint_name = get_joint_name(world.kitchen, joint)
        if joint_name in DRAWERS:
            world.open_door(joint)
        else:
            world.close_door(joint)
    for name in world.movable:
        set_pose(world.get_body(name), Pose(Point(z=-5.0)))

def get_ofree_ray_pose_test(world, **kwargs):
    # TODO: detect the configuration of joints
    def test(detect, obj_name, pose):
        if detect.name == obj_name:
            return True
        if isinstance(pose, SurfaceDist):
            return True
        move_occluding(world)
        detect.pose.assign()
        pose.assign()
        body = world.get_body(detect.name)
        obstacles = get_link_obstacles(world, obj_name)
        if any(pairwise_collision(body, obst) for obst in obstacles):
            return False
        visible = not obstacles & detect.compute_occluding()
        #if not visible:
        #    handles = detect.draw()
        #    wait_for_user()
        #    remove_handles(handles)
        return visible
    return test

def get_ofree_ray_grasp_test(world, **kwargs):
    def test(detect, bconf, aconf, obj_name, grasp):
        if detect.name == obj_name:
            return True
        move_occluding(world)
        bconf.assign()
        aconf.assign()
        detect.pose.assign()
        if obj_name is not None:
            grasp.assign()
            obstacles = get_link_obstacles(world, obj_name)
        else:
            obstacles = get_descendant_obstacles(world.robot)
        visible = not obstacles & detect.compute_occluding()
        #if not visible:
        #    handles = detect.draw()
        #    wait_for_user()
        #    remove_handles(handles)
        return visible
    return test

class Observation(object):
    # Primary motivation is to seperate the object
    def __init__(self, value):
        self.value = value
    def __repr__(self):
        return 'obs({})'.format(self.value)

def get_sample_belief_gen(world, # min_prob=1. / NUM_PARTICLES,  # TODO: relative instead?
                          mlo_only=False, ordered=False, **kwargs):
    # TODO: incorporate ray tracing
    detect_fn = get_compute_detect(world, ray_trace=False, **kwargs)
    def gen(obj_name, pose_dist, surface_name):
        # TODO: apply these checks to the whole surfaces
        valid_samples = {}
        for rp in pose_dist.dist.support():
            prob = pose_dist.discrete_prob(rp)
            obs = None
            cost = detect_cost_fn(obj_name, pose_dist, obs, rp)
            if (cost < MAX_COST): # and (min_prob < prob):
                # pose = rp.get_world_from_body()
                result = detect_fn(obj_name, rp)
                if result is not None:
                    # detect, = result
                    # detect.draw()
                    valid_samples[rp] = prob
        if not valid_samples:
            return

        if mlo_only:
            rp = max(valid_samples, key=valid_samples.__getitem__)
            obs = Observation(rp)
            yield (obs,)
            return
        if ordered:
            for rp in sorted(valid_samples, key=valid_samples.__getitem__, reverse=True):
                obs = Observation(rp)
                yield (obs,)
        else:
            while valid_samples:
                dist = DDist(valid_samples)
                rp = dist.sample()
                del valid_samples[rp]
                obs = Observation(rp)
                yield (obs,)
    return gen

def update_belief_fn(world, **kwargs):
    def fn(obj_name, pose_dist, surface_name, obs):
        rp = obs.value # TODO: proper Bayesian update
        return (rp,)
    return fn

################################################################################

def get_test_near_pose(world, grow_entity=GROW_FORWARD_RADIUS, collisions=False, teleport=False, **kwargs):
    base_from_objects = grow_polygon(map(point_from_pose, load_forward_placements(world, **kwargs)), radius=grow_entity)
    vertices_from_surface = {}
    # TODO: alternatively, distance to hull

    def test(object_name, pose, base_conf):
        if object_name in ALL_SURFACES:
            surface_name = object_name
            if surface_name not in vertices_from_surface:
                vertices_from_surface[surface_name] = grow_polygon(
                    map(point_from_pose, load_inverse_placements(world, surface_name)), radius=GROW_INVERSE_BASE)
            if not vertices_from_surface[surface_name]:
                return False
            base_conf.assign()
            pose.assign()
            surface = surface_from_name(surface_name)
            world_from_surface = get_link_pose(world.kitchen, link_from_name(world.kitchen, surface.link))
            world_from_base = get_link_pose(world.robot, world.base_link)
            surface_from_base = multiply(invert(world_from_surface), world_from_base)
            #result = is_point_in_polygon(point_from_pose(surface_from_base), vertices_from_surface[surface_name])
            #if not result:
            #    draw_pose(surface_from_base)
            #    points = [Point(x, y, 0) for x, y, in vertices_from_surface[surface_name]]
            #    add_segments(points, closed=True)
            #    wait_for_user()
            return is_point_in_polygon(point_from_pose(surface_from_base), vertices_from_surface[surface_name])
        else:
            if not base_from_objects:
                return False
            base_conf.assign()
            pose.assign()
            world_from_base = get_link_pose(world.robot, world.base_link)
            world_from_object = pose.get_world_from_body()
            base_from_object = multiply(invert(world_from_base), world_from_object)
            return is_point_in_polygon(point_from_pose(base_from_object), base_from_objects)
    return test

def get_test_near_joint(world, **kwargs):
    vertices_from_joint = {}

    def test(joint_name, base_conf):
        if joint_name in KNOBS:
            return True # TODO: address this
        if joint_name not in vertices_from_joint:
            base_confs = list(load_pull_base_poses(world, joint_name))
            vertices_from_joint[joint_name] = grow_polygon(base_confs, radius=GROW_INVERSE_BASE)
        if not vertices_from_joint[joint_name]:
            return False
        # TODO: can't open hitman_drawer_top_joint any more
        # Likely due to conservative carter geometry
        base_conf.assign()
        base_point = point_from_pose(get_link_pose(world.robot, world.base_link))
        return is_point_in_polygon(base_point[:2], vertices_from_joint[joint_name])
    return test

################################################################################

def get_stable_gen(world, max_attempts=100,
                   learned=True, collisions=True,
                   pos_scale=0.01, rot_scale=np.pi/16,
                   z_offset=Z_EPSILON, **kwargs):

    # TODO: remove fixed collisions with contained surfaces
    # TODO: place where currently standing
    def gen(obj_name, surface_name):
        obj_body = world.get_body(obj_name)
        surface_body = world.kitchen
        if surface_name in ENV_SURFACES:
            surface_body = world.environment_bodies[surface_name]
        surface_aabb = compute_surface_aabb(world, surface_name)
        learned_poses = load_placements(world, surface_name) if learned else [] # TODO: GROW_PLACEMENT
        while True:
            for _ in range(max_attempts):
                if surface_name in STOVES:
                    surface_link = link_from_name(world.kitchen, surface_name)
                    world_from_surface = get_link_pose(world.kitchen, surface_link)
                    z = stable_z_on_aabb(obj_body, surface_aabb) - point_from_pose(world_from_surface)[2]
                    theta = random.uniform(-np.pi, +np.pi)
                    body_pose_surface = Pose(Point(z=z + z_offset), Euler(yaw=theta))
                    body_pose_world = multiply(world_from_surface, body_pose_surface)
                elif learned:
                    if not learned_poses:
                        return
                    surface_pose_world = get_surface_reference_pose(surface_body, surface_name)
                    sampled_pose_surface = multiply(surface_pose_world, random.choice(learned_poses))
                    [x, y, _] = point_from_pose(sampled_pose_surface)
                    _, _, yaw = euler_from_quat(quat_from_pose(sampled_pose_surface))
                    dx, dy = np.random.normal(scale=pos_scale, size=2)
                    # TODO: avoid reloading
                    z = stable_z_on_aabb(obj_body, surface_aabb)
                    theta = wrap_angle(yaw + np.random.normal(scale=rot_scale))
                    #yaw = np.random.uniform(*CIRCULAR_LIMITS)
                    quat = quat_from_euler(Euler(yaw=theta))
                    body_pose_world = ([x+dx, y+dy, z+z_offset], quat)
                    # TODO: project onto the surface
                else:
                    # TODO: halton sequence
                    # unit_generator(d, use_halton=True)
                    body_pose_world = sample_placement_on_aabb(obj_body, surface_aabb, epsilon=z_offset)
                    if body_pose_world is None:
                        continue # return?
                set_pose(obj_body, body_pose_world)
                # TODO: make sure the surface is open when doing this
                if test_supported(world, obj_body, surface_name, collisions=collisions):
                    rp = create_relative_pose(world, obj_name, surface_name)
                    yield (rp,)
                    break
            else:
                yield None
    return gen

def get_nearby_stable_gen(world, max_attempts=25, **kwargs):
    stable_gen = get_stable_gen(world, **kwargs)
    test_near_pose = get_test_near_pose(world, #surface_names=[],
                                        grasp_types=[TOP_GRASP], grow_entity=0.0)
    compute_pose_kin = get_compute_pose_kin(world)

    def gen(obj_name, surface_name, pose2, base_conf):
        #base_conf.assign()
        #pose2.assign()
        while True:
            for rel_pose, in islice(stable_gen(obj_name, surface_name), max_attempts):
                pose1, = compute_pose_kin(obj_name, rel_pose, surface_name, pose2)
                if test_near_pose(obj_name, pose1, base_conf):
                    yield (pose1, rel_pose)
                    break
            else:
                yield None
    return gen

def get_grasp_gen(world, collisions=False, randomize=True, **kwargs): # teleport=False,
    # TODO: produce carry arm confs here
    def gen(name, grasp_type):
        for grasp in get_grasps(world, name, grasp_types=[grasp_type], **kwargs):
            yield (grasp,)
    return gen

################################################################################

def inverse_reachability(world, base_generator, obstacles=set(),
                         max_attempts=50, min_distance=0.01, **kwargs):
    lower_limits, upper_limits = get_custom_limits(
        world.robot, world.base_joints, world.custom_limits)
    while True:
        for i, base_conf in enumerate(islice(base_generator, max_attempts)):
            if not all_between(lower_limits, base_conf, upper_limits):
                continue
            # TODO: account for doors and placed-object collisions here
            #pose.assign()
            bq = FConf(world.robot, world.base_joints, base_conf)
            bq.assign()
            for conf in world.special_confs:
                # TODO: ensure the base and/or end-effector is visible at the calibrate_conf
                # Could even sample a special visible conf for this base_conf
                conf.assign()
                if any(pairwise_collision(world.robot, b, max_distance=min_distance) for b in obstacles):
                    break
            else:
                # print('IR attempts:', i)
                yield (bq,)
                break
        else:
            if PRINT_FAILURES: print('Failed after {} IR attempts:'.format(max_attempts))
            return
            #yield None # Break or yield none?

def plan_approach(world, approach_pose, attachments=[], obstacles=set(),
                  teleport=False, switches_only=False,
                  approach_path=not MOVE_ARM, **kwargs):
    # TODO: use velocities in the distance function
    distance_fn = get_distance_fn(world.robot, world.arm_joints)
    aq = world.carry_conf
    grasp_conf = get_joint_positions(world.robot, world.arm_joints)
    if switches_only:
        return [aq.values, grasp_conf]

    # TODO: could extract out collision function
    # TODO: track the full approach motion
    full_approach_conf = world.solve_inverse_kinematics(
        approach_pose, nearby_tolerance=NEARBY_APPROACH)
    if full_approach_conf is None: # TODO: | {obj}
        if PRINT_FAILURES: print('Pregrasp kinematic failure')
        return None
    moving_links = get_moving_links(world.robot, world.arm_joints)
    robot_obstacle = (world.robot, frozenset(moving_links))
    #robot_obstacle = world.robot
    if any(pairwise_collision(robot_obstacle, b) for b in obstacles): # TODO: | {obj}
        if PRINT_FAILURES: print('Pregrasp collision failure')
        return None
    approach_conf = get_joint_positions(world.robot, world.arm_joints)
    if teleport:
        return [aq.values, approach_conf, grasp_conf]
    distance = distance_fn(grasp_conf, approach_conf)
    if MAX_CONF_DISTANCE < distance:
        if PRINT_FAILURES: print('Pregrasp proximity failure (distance={:.5f})'.format(distance))
        return None

    resolutions = ARM_RESOLUTION * np.ones(len(world.arm_joints))
    grasp_path = plan_direct_joint_motion(world.robot, world.arm_joints, grasp_conf,
                                          attachments=attachments, obstacles=obstacles,
                                          self_collisions=SELF_COLLISIONS,
                                          disabled_collisions=world.disabled_collisions,
                                          custom_limits=world.custom_limits, resolutions=resolutions / 4.)
    if grasp_path is None:
        if PRINT_FAILURES: print('Pregrasp path failure')
        return None
    if not approach_path:
        return grasp_path
    # TODO: plan one with attachment placed and one held
    # TODO: can still use this as a witness that the conf is reachable
    aq.assign()
    approach_path = plan_joint_motion(world.robot, world.arm_joints, approach_conf,
                                      attachments=attachments,
                                      obstacles=obstacles,
                                      self_collisions=SELF_COLLISIONS,
                                      disabled_collisions=world.disabled_collisions,
                                      custom_limits=world.custom_limits, resolutions=resolutions,
                                      restarts=2, iterations=25, smooth=25)
    if approach_path is None:
        if PRINT_FAILURES: print('Approach path failure')
        return None
    return approach_path + grasp_path

################################################################################

def is_approach_safe(world, obj_name, pose, grasp, obstacles):
    assert pose.support is not None
    obj_body = world.get_body(obj_name)
    pose.assign()  # May set the drawer confs as well
    set_joint_positions(world.gripper, get_movable_joints(world.gripper), world.open_gq.values)
    #set_renderer(enable=True)
    for _ in iterate_approach_path(world, pose, grasp, body=obj_body):
        #for link in get_all_links(world.gripper):
        #    set_color(world.gripper, apply_alpha(np.zeros(3)), link)
        #wait_for_user()
        if any(pairwise_collision(world.gripper, obst) # or pairwise_collision(obj_body, obst)
               for obst in obstacles):
            print('Unsafe approach!')
            return False
    return True

def plan_pick(world, obj_name, pose, grasp, base_conf, obstacles, randomize=True, **kwargs):
    # TODO: check if within database convex hull
    # TODO: flag to check if initially in collision

    obj_body = world.get_body(obj_name)
    pose.assign()
    base_conf.assign()
    world.open_gripper()
    robot_saver = BodySaver(world.robot)
    obj_saver = BodySaver(obj_body)

    if randomize:
        sample_fn = get_sample_fn(world.robot, world.arm_joints)
        set_joint_positions(world.robot, world.arm_joints, sample_fn())
    else:
        world.carry_conf.assign()
    world_from_body = pose.get_world_from_body()
    gripper_pose = multiply(world_from_body, invert(grasp.grasp_pose))  # w_f_g = w_f_o * (g_f_o)^-1
    full_grasp_conf = world.solve_inverse_kinematics(gripper_pose)
    if full_grasp_conf is None:
        if PRINT_FAILURES: print('Grasp kinematic failure')
        return
    moving_links = get_moving_links(world.robot, world.arm_joints)
    robot_obstacle = (world.robot, frozenset(moving_links))
    #robot_obstacle = get_descendant_obstacles(world.robot, child_link_from_joint(world.arm_joints[0]))
    #robot_obstacle = world.robot
    if any(pairwise_collision(robot_obstacle, b) for b in obstacles):
        if PRINT_FAILURES: print('Grasp collision failure')
        #set_renderer(enable=True)
        #wait_for_user()
        #set_renderer(enable=False)
        return
    approach_pose = multiply(world_from_body, invert(grasp.pregrasp_pose))
    approach_path = plan_approach(world, approach_pose,  # attachments=[grasp.get_attachment()],
                                  obstacles=obstacles, **kwargs)
    if approach_path is None:
        if PRINT_FAILURES: print('Approach plan failure')
        return
    if MOVE_ARM:
        aq = FConf(world.robot, world.arm_joints, approach_path[0])
    else:
        aq = world.carry_conf

    gripper_motion_fn = get_gripper_motion_gen(world, **kwargs)
    finger_cmd, = gripper_motion_fn(world.open_gq, grasp.get_gripper_conf())
    attachment = create_surface_attachment(world, obj_name, pose.support)
    cmd = Sequence(State(world, savers=[robot_saver, obj_saver],
                         attachments=[attachment]), commands=[
        ApproachTrajectory(world, world.robot, world.arm_joints, approach_path),
        finger_cmd.commands[0],
        Detach(world, attachment.parent, attachment.parent_link, attachment.child),
        AttachGripper(world, obj_body, grasp=grasp),
        ApproachTrajectory(world, world.robot, world.arm_joints, reversed(approach_path)),
    ], name='pick')
    yield (aq, cmd,)

################################################################################

def get_fixed_pick_gen_fn(world, max_attempts=25, collisions=True, **kwargs):

    def gen(obj_name, pose, grasp, base_conf):
        obstacles = world.static_obstacles | get_surface_obstacles(world, pose.support)  # | {obj_body}
        #if not collisions:
        #    obstacles = set()
        if not is_approach_safe(world, obj_name, pose, grasp, obstacles):
            return
        # TODO: increase timeouts if a previously successful value
        # TODO: seed IK using the previous solution
        while True:
            for i in range(max_attempts):
                randomize = (random.random() < P_RANDOMIZE_IK)
                ik_outputs = next(plan_pick(world, obj_name, pose, grasp, base_conf, obstacles,
                                            randomize=randomize, **kwargs), None)
                if ik_outputs is not None:
                    yield ik_outputs
                    break  # return
            else:
                if PRINT_FAILURES: print('Fixed pick failure')
                if not pose.init:
                    break
                yield None
    return gen

def get_pick_gen_fn(world, max_attempts=25, collisions=True, learned=True, **kwargs):
    # TODO: sample in the neighborhood of the base conf to ensure robust

    def gen(obj_name, pose, grasp, *args):
        obstacles = world.static_obstacles | get_surface_obstacles(world, pose.support)
        #if not collisions:
        #    obstacles = set()
        if not is_approach_safe(world, obj_name, pose, grasp, obstacles):
            return

        # TODO: check collisions with obj at pose
        gripper_pose = multiply(pose.get_world_from_body(), invert(grasp.grasp_pose)) # w_f_g = w_f_o * (g_f_o)^-1
        if learned:
            base_generator = cycle(load_place_base_poses(world, gripper_pose, pose.support, grasp.grasp_type))
        else:
            base_generator = uniform_pose_generator(world.robot, gripper_pose)
        safe_base_generator = inverse_reachability(world, base_generator, obstacles=obstacles, **kwargs)
        while True:
            for i in range(max_attempts):
                try:
                    base_conf, = next(safe_base_generator)
                except StopIteration:
                    return
                randomize = (random.random() < P_RANDOMIZE_IK)
                ik_outputs = next(plan_pick(world, obj_name, pose, grasp, base_conf, obstacles,
                                            randomize=randomize, **kwargs), None)
                if ik_outputs is not None:
                    yield (base_conf,) + ik_outputs
                    break
            else:
                if PRINT_FAILURES: print('Pick failure')
                if not pose.init:
                    break
                yield None
    return gen

################################################################################

HandleGrasp = namedtuple('HandleGrasp', ['link', 'handle_grasp', 'handle_pregrasp'])
DoorPath = namedtuple('DoorPath', ['link_path', 'handle_path', 'handle_grasp', 'tool_path'])

def get_handle_grasps(world, joint, pull=True, pre_distance=APPROACH_DISTANCE):
    pre_direction = pre_distance * get_unit_vector([0, 0, 1])
    #half_extent = 1.0*FINGER_EXTENT[2] # Collides
    half_extent = 1.05*FINGER_EXTENT[2]

    grasps = []
    for link in get_link_subtree(world.kitchen, joint):
        if 'handle' in get_link_name(world.kitchen, link):
            # TODO: can adjust the position and orientation on the handle
            # https://gitlab-master.nvidia.com/SRL/srl_system/blob/master/packages/brain/src/brain_ros/kitchen_poses.py
            for yaw in [0, np.pi]: # yaw=0 DOESN'T WORK WITH LULA
                handle_grasp = (Point(z=-half_extent), quat_from_euler(Euler(roll=np.pi, pitch=np.pi/2, yaw=yaw)))
                #if not pull:
                #    handle_pose = get_link_pose(world.kitchen, link)
                #    for distance in np.arange(0., 0.05, step=0.001):
                #        pregrasp = multiply(([0, 0, -distance], unit_quat()), handle_grasp)
                #        tool_pose = multiply(handle_pose, invert(pregrasp))
                #        set_tool_pose(world, tool_pose)
                #        # TODO: check collisions
                #        wait_for_user()
                handle_pregrasp = multiply((pre_direction, unit_quat()), handle_grasp)
                grasps.append(HandleGrasp(link, handle_grasp, handle_pregrasp))
    return grasps

def compute_door_paths(world, joint_name, door_conf1, door_conf2, obstacles=set(), teleport=False):
    door_paths = []
    if door_conf1 == door_conf2:
        return door_paths
    door_joint = joint_from_name(world.kitchen, joint_name)
    door_joints = [door_joint]
    # TODO: could unify with grasp path
    door_extend_fn = get_extend_fn(world.kitchen, door_joints, resolutions=[DOOR_RESOLUTION])
    door_path = [door_conf1.values] + list(door_extend_fn(door_conf1.values, door_conf2.values))
    if teleport:
        door_path = [door_conf1.values, door_conf2.values]
    # TODO: open until collision for the drawers

    pull = (door_path[0][0] < door_path[-1][0])
    # door_obstacles = get_descendant_obstacles(world.kitchen, door_joint)
    for handle_grasp in get_handle_grasps(world, door_joint, pull=pull):
        link, grasp, pregrasp = handle_grasp
        handle_path = []
        for door_conf in door_path:
            set_joint_positions(world.kitchen, door_joints, door_conf)
            # if any(pairwise_collision(door_obst, obst)
            #       for door_obst, obst in product(door_obstacles, obstacles)):
            #    return
            handle_path.append(get_link_pose(world.kitchen, link))
            # Collide due to adjacency

        # TODO: check pregrasp path as well
        # TODO: check gripper self-collisions with the robot
        set_configuration(world.gripper, world.open_gq.values)
        tool_path = [multiply(handle_pose, invert(grasp))
                     for handle_pose in handle_path]
        for i, tool_pose in enumerate(tool_path):
            set_joint_positions(world.kitchen, door_joints, door_path[i])
            set_tool_pose(world, tool_pose)
            # handles = draw_pose(handle_path[i], length=0.25)
            # handles.extend(draw_aabb(get_aabb(world.kitchen, link=link)))
            # wait_for_user()
            # for handle in handles:
            #    remove_debug(handle)
            if any(pairwise_collision(world.gripper, obst) for obst in obstacles):
                break
        else:
            door_paths.append(DoorPath(door_path, handle_path, handle_grasp, tool_path))
    return door_paths

def is_pull_safe(world, door_joint, door_plan):
    obstacles = get_descendant_obstacles(world.kitchen, door_joint)
    door_path, handle_path, handle_plan, tool_path = door_plan
    for door_conf in [door_path[0], door_path[-1]]:
        # TODO: check the whole door trajectory
        set_joint_positions(world.kitchen, [door_joint], door_conf)
        # TODO: just check collisions with the base of the robot
        if any(pairwise_collision(world.robot, b) for b in obstacles):
            if PRINT_FAILURES: print('Door start/end failure')
            return False
    return True

def plan_pull(world, door_joint, door_plan, base_conf,
              randomize=True, collisions=True, teleport=False, **kwargs):
    door_path, handle_path, handle_plan, tool_path = door_plan
    handle_link, handle_grasp, handle_pregrasp = handle_plan
    # TODO: could push if the goal is to be fully closed

    door_obstacles = get_descendant_obstacles(world.kitchen, door_joint) # if collisions else set()
    obstacles = (world.static_obstacles | door_obstacles) # if collisions else set()

    base_conf.assign()
    world.open_gripper()
    world.carry_conf.assign()
    robot_saver = BodySaver(world.robot) # TODO: door_saver?
    if not is_pull_safe(world, door_joint, door_plan):
        return

    # Assuming that pairs of fixed things aren't in collision at this point
    moving_links = get_moving_links(world.robot, world.arm_joints)
    robot_obstacle = (world.robot, frozenset(moving_links))
    distance_fn = get_distance_fn(world.robot, world.arm_joints)
    if randomize:
        sample_fn = get_sample_fn(world.robot, world.arm_joints)
        set_joint_positions(world.robot, world.arm_joints, sample_fn())
    else:
        world.carry_conf.assign()
    arm_path = []
    for i, tool_pose in enumerate(tool_path):
        set_joint_positions(world.kitchen, [door_joint], door_path[i])
        tolerance = INF if i == 0 else NEARBY_PULL
        full_arm_conf = world.solve_inverse_kinematics(tool_pose, nearby_tolerance=tolerance)
        if full_arm_conf is None:
            if PRINT_FAILURES: print('Door kinematic failure')
            return
        if any(pairwise_collision(robot_obstacle, b) for b in obstacles):
            if PRINT_FAILURES: print('Door collision failure')
            return
        arm_conf = get_joint_positions(world.robot, world.arm_joints)
        if arm_path and not teleport:
            distance = distance_fn(arm_path[-1], arm_conf)
            if MAX_CONF_DISTANCE < distance:
                if PRINT_FAILURES: print('Door proximity failure (distance={:.5f})'.format(distance))
                return
        arm_path.append(arm_conf)
        # wait_for_user()

    approach_paths = []
    for index in [0, -1]:
        set_joint_positions(world.kitchen, [door_joint], door_path[index])
        set_joint_positions(world.robot, world.arm_joints, arm_path[index])
        tool_pose = multiply(handle_path[index], invert(handle_pregrasp))
        approach_path = plan_approach(world, tool_pose, obstacles=obstacles, teleport=teleport, **kwargs)
        if approach_path is None:
            return
        approach_paths.append(approach_path)

    if MOVE_ARM:
        aq1 = FConf(world.robot, world.arm_joints, approach_paths[0][0])
        aq2 = FConf(world.robot, world.arm_joints, approach_paths[-1][0])
    else:
        aq1 = world.carry_conf
        aq2 = aq1

    set_joint_positions(world.kitchen, [door_joint], door_path[0])
    set_joint_positions(world.robot, world.arm_joints, arm_path[0])
    grasp_width = close_until_collision(world.robot, world.gripper_joints,
                                        bodies=[(world.kitchen, [handle_link])])
    gripper_motion_fn = get_gripper_motion_gen(world, teleport=teleport, collisions=collisions, **kwargs)
    gripper_conf = FConf(world.robot, world.gripper_joints, [grasp_width] * len(world.gripper_joints))
    finger_cmd, = gripper_motion_fn(world.open_gq, gripper_conf)

    commands = [
        ApproachTrajectory(world, world.robot, world.arm_joints, approach_paths[0]),
        DoorTrajectory(world, world.robot, world.arm_joints, arm_path,
                       world.kitchen, [door_joint], door_path),
        ApproachTrajectory(world, world.robot, world.arm_joints, reversed(approach_paths[-1])),
    ]
    door_path, _, _, _ = door_plan
    pull = (door_path[0][0] < door_path[-1][0])
    if pull:
        commands.insert(1, finger_cmd.commands[0])
        commands.insert(3, finger_cmd.commands[0].reverse())
    cmd = Sequence(State(world, savers=[robot_saver]), commands, name='pull')
    yield (aq1, aq2, cmd,)

################################################################################

def get_fixed_pull_gen_fn(world, max_attempts=25, collisions=True, teleport=False, **kwargs):

    def gen(joint_name, door_conf1, door_conf2, base_conf):
        #if door_conf1 == door_conf2:
        #    return
        # TODO: check if within database convex hull
        door_joint = joint_from_name(world.kitchen, joint_name)
        obstacles = (world.static_obstacles | get_descendant_obstacles(
            world.kitchen, door_joint)) # if collisions else set()

        base_conf.assign()
        world.carry_conf.assign()
        door_plans = [door_plan for door_plan in compute_door_paths(
            world, joint_name, door_conf1, door_conf2, obstacles, teleport=teleport)
                      if is_pull_safe(world, door_joint, door_plan)]
        if not door_plans:
            print('Unable to open door {} at fixed config'.format(joint_name))
            return
        while True:
            for i in range(max_attempts):
                door_path = random.choice(door_plans)
                # TracIK is itself stochastic
                randomize = (random.random() < P_RANDOMIZE_IK)
                ik_outputs = next(plan_pull(world, door_joint, door_path, base_conf,
                                            randomize=randomize, collisions=collisions, teleport=teleport, **kwargs),
                                  None)
                if ik_outputs is not None:
                    yield ik_outputs
                    break  # return
            else:
                if PRINT_FAILURES: print('Fixed pull failure')
                yield None
    return gen

def get_pull_gen_fn(world, max_attempts=50, collisions=True, teleport=False, learned=True, **kwargs):
    # TODO: could condition pick/place into cabinet on the joint angle
    obstacles = world.static_obstacles
    #if not collisions:
    #    obstacles = set()

    def gen(joint_name, door_conf1, door_conf2, *args):
        if door_conf1 == door_conf2:
            return
        door_joint = joint_from_name(world.kitchen, joint_name)
        door_paths = compute_door_paths(world, joint_name, door_conf1, door_conf2, obstacles, teleport=teleport)
        if not door_paths:
            return
        if learned:
            base_generator = cycle(load_pull_base_poses(world, joint_name))
        else:
            _, _, _, tool_path = door_paths[0]
            index = int(len(tool_path) / 2)  # index = 0
            target_pose = tool_path[index]
            base_generator = uniform_pose_generator(world.robot, target_pose)
        safe_base_generator = inverse_reachability(world, base_generator, obstacles=obstacles, **kwargs)
        while True:
            for i in range(max_attempts):
                try:
                    base_conf, = next(safe_base_generator)
                except StopIteration:
                    return
                door_path = random.choice(door_paths)
                randomize = (random.random() < P_RANDOMIZE_IK)
                ik_outputs = next(plan_pull(world, door_joint, door_path, base_conf,
                                            randomize=randomize, collisions=collisions, teleport=teleport, **kwargs), None)
                if ik_outputs is not None:
                    yield (base_conf,) + ik_outputs
                    break
            else:
                if PRINT_FAILURES: print('Pull failure')
                yield None
    return gen

################################################################################

def plan_press(world, knob_name, pose, grasp, base_conf, obstacles, randomize=True, **kwargs):
    base_conf.assign()
    world.close_gripper()
    robot_saver = BodySaver(world.robot)

    if randomize:
        sample_fn = get_sample_fn(world.robot, world.arm_joints)
        set_joint_positions(world.robot, world.arm_joints, sample_fn())
    else:
        world.carry_conf.assign()
    gripper_pose = multiply(pose, invert(grasp.grasp_pose))  # w_f_g = w_f_o * (g_f_o)^-1
    #set_joint_positions(world.gripper, get_movable_joints(world.gripper), world.closed_gq.values)
    #set_tool_pose(world, gripper_pose)
    full_grasp_conf = world.solve_inverse_kinematics(gripper_pose)
    #wait_for_user()
    if full_grasp_conf is None:
        # if PRINT_FAILURES: print('Grasp kinematic failure')
        return
    robot_obstacle = (world.robot, frozenset(get_moving_links(world.robot, world.arm_joints)))
    if any(pairwise_collision(robot_obstacle, b) for b in obstacles):
        #if PRINT_FAILURES: print('Grasp collision failure')
        return
    approach_pose = multiply(pose, invert(grasp.pregrasp_pose))
    approach_path = plan_approach(world, approach_pose, obstacles=obstacles, **kwargs)
    if approach_path is None:
        return
    aq = FConf(world.robot, world.arm_joints, approach_path[0]) if MOVE_ARM else world.carry_conf

    gripper_motion_fn = get_gripper_motion_gen(world, **kwargs)
    finger_cmd, = gripper_motion_fn(world.open_gq, world.closed_gq)
    cmd = Sequence(State(world, savers=[robot_saver]), commands=[
        finger_cmd.commands[0],
        ApproachTrajectory(world, world.robot, world.arm_joints, approach_path),
        ApproachTrajectory(world, world.robot, world.arm_joints, reversed(approach_path)),

        finger_cmd.commands[0].reverse(),
    ], name='press')
    yield (aq, cmd,)

def get_grasp_presses(world, knob, pre_distance=APPROACH_DISTANCE):
    knob_link = link_from_name(world.kitchen, knob)
    pre_direction = pre_distance * get_unit_vector([0, 0, 1])
    post_direction = unit_point()
    for i, grasp_pose in enumerate(get_top_presses(world.kitchen, link=knob_link,
                                                   tool_pose=TOOL_POSE, top_offset=FINGER_EXTENT[0]/2 + 5e-3)):
        pregrasp_pose = multiply(Pose(point=pre_direction), grasp_pose,
                                 Pose(point=post_direction))
        grasp = Grasp(world, knob, TOP_GRASP, i, grasp_pose, pregrasp_pose)
        yield grasp

def get_press_gen_fn(world, max_attempts=50, collisions=True, teleport=False, learned=False, **kwargs):
    def gen(knob_name):
        obstacles = world.static_obstacles
        knob_link = link_from_name(world.kitchen, knob_name)
        pose = get_link_pose(world.kitchen, knob_link)
        #pose = RelPose(world.kitchen, knob_link, init=True)
        presses = cycle(get_grasp_presses(world, knob_name))
        grasp = next(presses)
        gripper_pose = multiply(pose, invert(grasp.grasp_pose)) # w_f_g = w_f_o * (g_f_o)^-1
        if learned:
            #base_generator = cycle(load_place_base_poses(world, gripper_pose, pose.support, grasp.grasp_type))
            raise NotImplementedError()
        else:
            base_generator = uniform_pose_generator(world.robot, gripper_pose)
        safe_base_generator = inverse_reachability(world, base_generator, obstacles=obstacles, **kwargs)
        while True:
            for i in range(max_attempts):
                try:
                    base_conf, = next(safe_base_generator)
                except StopIteration:
                    return
                grasp = next(presses)
                randomize = (random.random() < P_RANDOMIZE_IK)
                ik_outputs = next(plan_press(world, knob_name, pose, grasp, base_conf, obstacles,
                                             randomize=randomize, **kwargs), None)
                if ik_outputs is not None:
                    yield (base_conf,) + ik_outputs
                    break
            else:
                if PRINT_FAILURES: print('Pick failure')
                if not pose.init:
                    break
                yield None
    return gen

def get_fixed_press_gen_fn(world, max_attempts=25, collisions=True, teleport=False, **kwargs):

    def gen(knob_name, base_conf):
        knob_link = link_from_name(world.kitchen, knob_name)
        pose = get_link_pose(world.kitchen, knob_link)
        presses = cycle(get_grasp_presses(world, knob_name))
        while True:
            for i in range(max_attempts):
                grasp = next(presses)
                randomize = (random.random() < P_RANDOMIZE_IK)
                ik_outputs = next(plan_press(world, knob_name, pose, grasp, base_conf, world.static_obstacles,
                                             randomize=randomize, **kwargs), None)
                if ik_outputs is not None:
                    yield ik_outputs
                    break  # return
            else:
                if PRINT_FAILURES: print('Fixed pull failure')
                yield None
    return gen

################################################################################

def parse_fluents(world, fluents):
    attachments = []
    obstacles = set()
    for fluent in fluents:
        predicate, args = fluent[0], fluent[1:]
        if predicate in {p.lower() for p in ['AtBConf', 'AtAConf', 'AtGConf']}:
            q, = args
            q.assign()
        elif predicate == 'AtAngle'.lower():
            raise RuntimeError()
            # j, a = args
            # a.assign()
            # obstacles.update(get_descendant_obstacles(a.body, a.joints[0]))
        elif predicate in {p.lower() for p in ['AtPose', 'AtWorldPose']}:
            b, p = args
            if isinstance(p, SurfaceDist):
                continue
            p.assign()
            obstacles.update(get_link_obstacles(world, b))
        elif predicate == 'AtGrasp'.lower():
            b, g = args
            if b is not None:
                attachments.append(g.get_attachment())
                attachments[-1].assign()
        else:
            raise NotImplementedError(predicate)
    return attachments, obstacles

def get_base_motion_fn(world, teleport_base=False, collisions=True, teleport=False,
                       restarts=4, iterations=75, smooth=100):
    # TODO: lazy planning on a common base roadmap

    def fn(bq1, bq2, aq, fluents=[]):
        #if bq1 == bq2:
        #    return None
        bq1.assign()
        aq.assign()
        attachments, obstacles = parse_fluents(world, fluents)
        if not collisions:
            obstacles = set()
        obstacles.update(world.static_obstacles)
        robot_saver = BodySaver(world.robot)
        if (bq1 == bq2) or teleport_base or teleport:
            path = [bq1.values, bq2.values]
        else:
            # It's important that the extend function is reversible to avoid getting trapped
            path = plan_nonholonomic_motion(world.robot, bq2.joints, bq2.values, attachments=attachments,
                                            obstacles=obstacles, custom_limits=world.custom_limits,
                                            reversible=True, self_collisions=False,
                                            restarts=restarts, iterations=iterations, smooth=smooth)
            if path is None:
                print('Failed to find a base motion plan!')
                if PAUSE_MOTION_FAILURES:
                    set_renderer(enable=True)
                    #print(fluents)
                    for bq in [bq1, bq2]:
                        bq.assign()
                        wait_for_user()
                    set_renderer(enable=False)
                return None
        # TODO: could actually plan with all joints as long as we return to the same config
        cmd = Sequence(State(world, savers=[robot_saver]), commands=[
            Trajectory(world, world.robot, world.base_joints, path),
        ], name='base')
        return (cmd,)
    return fn

def get_reachability_test(world, **kwargs):
    base_motion_fn = get_base_motion_fn(world, restarts=2, iterations=50, smooth=0, **kwargs)
    bq0 = FConf(world.robot, world.base_joints)
    # TODO: can check for arm motions as well

    def test(bq):
        aq = world.carry_conf
        outputs = base_motion_fn(aq, bq0, bq, fluents=[])
        return outputs is not None
    return test

################################################################################

def get_arm_motion_gen(world, collisions=True, teleport=False):
    resolutions = ARM_RESOLUTION * np.ones(len(world.arm_joints))

    def fn(bq, aq1, aq2, fluents=[]):
        #if aq1 == aq2:
        #    return None
        bq.assign()
        aq1.assign()
        attachments, obstacles = parse_fluents(world, fluents)
        if not collisions:
            obstacles = set()
        obstacles.update(world.static_obstacles)
        robot_saver = BodySaver(world.robot)
        if teleport:
            path = [aq1.values, aq2.values]
        else:
            path = plan_joint_motion(world.robot, aq2.joints, aq2.values,
                                     attachments=attachments, obstacles=obstacles,
                                     self_collisions=SELF_COLLISIONS,
                                     disabled_collisions=world.disabled_collisions,
                                     custom_limits=world.custom_limits, resolutions=resolutions,
                                     restarts=2, iterations=50, smooth=50)
            if path is None:
                print('Failed to find an arm motion plan!')
                if PAUSE_MOTION_FAILURES:
                    set_renderer(enable=True)
                    #print(fluents)
                    for bq in [aq1, aq2]:
                        bq.assign()
                        wait_for_user()
                    set_renderer(enable=False)
                return None
        cmd = Sequence(State(world, savers=[robot_saver]), commands=[
            Trajectory(world, world.robot, world.arm_joints, path),
        ], name='arm')
        return (cmd,)
    return fn

def get_gripper_motion_gen(world, teleport=False, **kwargs):
    resolutions = GRIPPER_RESOLUTION * np.ones(len(world.gripper_joints))

    def fn(gq1, gq2):
        #if gq1 == gq2:
        #    return None
        if teleport:
            path = [gq1.values, gq2.values]
        else:
            extend_fn = get_extend_fn(gq2.body, gq2.joints, resolutions=resolutions)
            path = [gq1.values] + list(extend_fn(gq1.values, gq2.values))
        cmd = Sequence(State(world), commands=[
            Trajectory(world, gq2.body, gq2.joints, path),
        ], name='gripper')
        return (cmd,)
    return fn

################################################################################

def get_calibrate_gen(world, collisions=True, teleport=False):

    def fn(bq, *args): #, aq):
        # TODO: include if holding anything?
        bq.assign()
        aq = world.carry_conf
        #aq.assign() # TODO: could sample aq instead achieve it by move actions
        #world.open_gripper()
        robot_saver = BodySaver(world.robot)
        cmd = Sequence(State(world, savers=[robot_saver]), commands=[
            #Trajectory(world, world.robot, world.arm_joints, approach_path),
            # TODO: calibrate command
        ], name='calibrate')
        return (cmd,)
    return fn

################################################################################

OPEN = 'open'
CLOSED = 'closed'
DOOR_STATUSES = [OPEN, CLOSED]
# TODO: retrieve from entity

def get_gripper_open_test(world, error_percent=0.1): #, tolerance=1e-2
    open_gq = error_percent * np.array(world.closed_gq.values) + \
              (1 - error_percent) * np.array(world.open_gq.values)
    #open_gq = world.open_gq.values - tolerance * np.ones(len(world.gripper_joints))
    def test(gq):
        #if gq == world.open_gq:
        #    print('Initial grasp:', gq)
        return np.less_equal(open_gq, gq.values).all()
    return test

def get_door_test(world, error_percent=0.35): #, tolerance=1e-2):
    # TODO: separate error for open/closed
    def test(joint_name, conf, status):
        [joint] = conf.joints
        sign = world.get_door_sign(joint)
        #print(joint_name, world.closed_conf(joint), conf.values[0],
        #      world.open_conf(joint), status)
        position = sign*conf.values[0]
        if status == OPEN:
            open_position = sign * (error_percent * world.closed_conf(joint) +
                                    (1 - error_percent) * world.open_conf(joint))
            #open_position = sign * world.open_conf(joint) - tolerance
            return open_position <= position
        elif status == CLOSED:
            closed_position = sign * ((1 - error_percent) * world.closed_conf(joint) +
                                      error_percent * world.open_conf(joint))
            #closed_position = sign * world.closed_conf(joint) + tolerance
            return position <= closed_position
        raise NotImplementedError(status)
    return test

################################################################################

def get_cfree_pose_pose_test(world, collisions=True, **kwargs):
    def test(o1, rp1, o2, rp2, s):
        if not collisions or (o1 == o2):
            return True
        if isinstance(rp1, SurfaceDist) or isinstance(rp2, SurfaceDist):
            return True # TODO: perform this probabilistically
        rp1.assign()
        rp2.assign()
        return not pairwise_collision(world.get_body(o1), world.get_body(o2))
    return test

def get_cfree_worldpose_test(world, collisions=True, **kwargs):
    def test(o1, wp1):
        if isinstance(wp1, SurfaceDist):
            return True
        if not collisions or (wp1.support not in DRAWERS):
            return True
        body = world.get_body(o1)
        wp1.assign()
        obstacles = world.static_obstacles
        if any(pairwise_collision(body, obst) for obst in obstacles):
            return False
        return True
    return test

def get_cfree_worldpose_worldpose_test(world, collisions=True, **kwargs):
    def test(o1, wp1, o2, wp2):
        if isinstance(wp1, SurfaceDist) or isinstance(wp2, SurfaceDist):
            return True
        if not collisions or (o1 == o2) or (o2 == wp1.support): # DRAWERS
            return True
        body = world.get_body(o1)
        wp1.assign()
        wp2.assign()
        if any(pairwise_collision(body, obst) for obst in get_surface_obstacles(world, o2)):
            return False
        return True
    return test

def get_cfree_bconf_pose_test(world, collisions=True, **kwargs):
    def test(bq, o2, wp2):
        if not collisions:
            return True
        if isinstance(wp2, SurfaceDist):
            return True # TODO: perform this probabilistically
        bq.assign()
        world.carry_conf.assign()
        wp2.assign()
        obstacles = get_link_obstacles(world, o2)
        return not any(pairwise_collision(world.robot, obst) for obst in obstacles)
    return test

def get_cfree_approach_pose_test(world, collisions=True, **kwargs):
    def test(o1, wp1, g1, o2, wp2):
        # o1 will always be a movable object
        if isinstance(wp2, SurfaceDist):
            return True # TODO: perform this probabilistically
        if not collisions or (o1 == o2) or (o2 == wp1.support):
            return True
        # TODO: could define these on sets of samples to prune all at once
        body = world.get_body(o1)
        wp2.assign()
        obstacles = get_link_obstacles(world, o2) # - {body}
        if not obstacles:
            return True
        for _ in iterate_approach_path(world, wp1, g1, body=body):
            if any(pairwise_collision(part, obst) for part in
                   [world.gripper, body] for obst in obstacles):
                # TODO: some collisions the bottom drawer and the top drawer handle
                #print(o1, wp1.support, o2, wp2.support)
                #wait_for_user()
                return False
        return True
    return test

def get_cfree_angle_angle_test(world, collisions=True, **kwargs):
    def test(j1, a1, a2, o2, wp):
        if not collisions or (o2 in j1): # (j1 == JOINT_TEMPLATE.format(o2)):
            return True
        # TODO: check pregrasp path as well
        # TODO: pull path collisions
        wp.assign()
        set_configuration(world.gripper, world.open_gq.values)
        return compute_door_paths(world, j1, a1, a2, obstacles=get_link_obstacles(world, o2))
    return test

################################################################################

def get_cfree_traj_pose_test(world, collisions=True, **kwargs):
    def test(at, o, p):
        if not collisions:
            return True
        # TODO: check door collisions
        # TODO: still need to check static links at least once
        if isinstance(p, SurfaceDist):
            return True # TODO: perform this probabilistically
        p.assign()
        state = at.context.copy()
        state.assign()
        all_bodies = {body for command in at.commands for body in command.bodies}
        for command in at.commands:
            obstacles = get_link_obstacles(world, o) - all_bodies
            # TODO: why did I previously remove o at p?
            #obstacles = get_link_obstacles(world, o) - command.bodies  # - p.bodies # Doesn't include o at p
            if not obstacles:
                continue
            for _ in command.iterate(state):
                state.derive()
                # TODO: annote the surface in question
                #for attachment in state.attachments.values():
                #    if any(pairwise_collision(attachment.child, obst) for obst in obstacles):
                #        return False
                # TODO: just check collisions with moving links
                if any(pairwise_collision(world.robot, obst) for obst in obstacles):
                    #print(at, o, p)
                    #wait_for_user()
                    return False
        return True
    return test

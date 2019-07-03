import copy
import random
import numpy as np

from itertools import islice

from pybullet_tools.pr2_primitives import Conf
from pybullet_tools.utils import pairwise_collision, multiply, invert, get_joint_positions, BodySaver, get_distance, set_joint_positions, plan_direct_joint_motion, plan_joint_motion, \
    get_custom_limits, all_between, uniform_pose_generator, plan_nonholonomic_motion, link_from_name, get_max_limit, \
    get_extend_fn, joint_from_name, get_link_subtree, get_link_name, get_link_pose, \
    get_aabb, unit_point, Euler, quat_from_euler, read_obj, set_pose, has_link, \
    tform_mesh, point_from_pose, aabb_from_points, get_data_pose, sample_placement_on_aabb, get_sample_fn, \
    stable_z_on_aabb, is_placed_on_aabb, euler_from_quat, quat_from_pose, wrap_angle, \
    get_distance_fn, get_unit_vector, unit_quat, get_collision_data, \
    child_link_from_joint, create_attachment, Point

from utils import get_grasps, iterate_approach_path, \
    set_tool_pose, close_until_collision, get_descendant_obstacles, SURFACE_TOP, \
    SURFACE_BOTTOM, get_surface, SURFACE_FROM_NAME, CABINET_JOINTS, RelPose, FINGER_EXTENT
from command import Sequence, Trajectory, Attach, Detach, State, DoorTrajectory
from database import load_placements, get_surface_reference_pose, load_place_base_poses, load_pull_base_poses


BASE_CONSTANT = 1
BASE_VELOCITY = 0.25
SELF_COLLISIONS = False # TODO: include self-collisions
MAX_CONF_DISTANCE = 0.75

# TODO: need to wrap trajectory when executing in simulation or running on the robot

################################################################################

def base_cost_fn(q1, q2):
    distance = get_distance(q1.values[:2], q2.values[:2])
    return BASE_CONSTANT + distance / BASE_VELOCITY

def trajectory_cost_fn(t):
    distance = t.distance(distance_fn=lambda q1, q2: get_distance(q1[:2], q2[:2]))
    return BASE_CONSTANT + distance / BASE_VELOCITY

################################################################################

# TODO: more general forward kinematics

def get_compute_pose_kin(world):
    def fn(o1, rp, o2, p2):
        if o1 == o2:
            return None
        #if np.allclose(p2.value, unit_pose()):
        #    return (rp,)
        #if np.allclose(rp.value, unit_pose()):
        #    return (p2,)
        # TODO: assert that the links align?
        body = world.get_body(o1)
        p1 = RelPose(body, reference_body=p2.reference_body, reference_link=p2.reference_link,
                     support=rp.support, confs=(p2.confs + rp.confs), init=(rp.init and p2.init))
        return (p1,)
    return fn

def get_compute_angle_kin(world):
    def fn(o, j, a):
        link = link_from_name(world.kitchen, o) # link not surface
        p = RelPose(world.kitchen, link, confs=[a], init=a.init)
        return (p,)
    return fn

################################################################################

def compute_surface_aabb(world, name):
    surface_name, shape_name, _ = get_surface(name)
    surface_link = link_from_name(world.kitchen, surface_name)
    surface_pose = get_link_pose(world.kitchen, surface_link)
    if shape_name == SURFACE_TOP:
        surface_aabb = get_aabb(world.kitchen, surface_link)
    elif shape_name == SURFACE_BOTTOM:
        #data = sorted(get_collision_data(world.kitchen, surface_link),
        #              key=lambda d: point_from_pose(get_data_pose(d))[2])[0]
        raise NotImplementedError(shape_name)
    else:
        [data] = filter(lambda d: d.filename != '',
                        get_collision_data(world.kitchen, surface_link))
        local_pose = get_data_pose(data)
        meshes = read_obj(data.filename)
        #colors = spaced_colors(len(meshes))
        #set_color(world.kitchen, link=surface_link, color=np.zeros(4))
        mesh = meshes[shape_name]
        #for i, (name, mesh) in enumerate(meshes.items()):
        mesh = tform_mesh(multiply(surface_pose, local_pose), mesh=mesh)
        surface_aabb = aabb_from_points(mesh.vertices)
        #add_text(surface_name, position=surface_aabb[1])
        #draw_mesh(mesh, color=colors[i])
        #wait_for_user()
    #draw_aabb(surface_aabb)
    #wait_for_user()
    return surface_aabb

################################################################################

def get_surface_obstacles(world, surface_name):
    surface = get_surface(surface_name)
    obstacles = set()
    for joint_name in surface.joints:
        joint = joint_from_name(world.kitchen, joint_name)
        if joint_name in CABINET_JOINTS:
            # TODO: remove this mechanic in the future
            world.open_door(joint)
        link = child_link_from_joint(joint)
        obstacles.update(get_descendant_obstacles(world.kitchen, link))
    # Be careful to call this before each check
    return obstacles

def get_link_obstacles(world, link_name):
    if link_name in world.movable:
        return {world.get_body(link_name)}
    elif has_link(world.kitchen, link_name):
        link = link_from_name(world.kitchen, link_name)
        return get_descendant_obstacles(world.kitchen, link)
    assert link_name in SURFACE_FROM_NAME
    return set()

################################################################################

def test_supported(world, body, surface_name, collisions=True):
    surface_aabb = compute_surface_aabb(world, surface_name)
    if not is_placed_on_aabb(body, surface_aabb):  # , above_epsilon=z_offset+1e-3):
        return False
    obstacles = world.static_obstacles | get_surface_obstacles(world, surface_name)
    if not collisions:
        obstacles = set()
    return not any(pairwise_collision(body, obst) for obst in obstacles)

def get_stable_gen(world, learned=True, collisions=True, pos_scale=0.01, rot_scale=np.pi/16,
                   z_offset=5e-3, **kwargs):
    # TODO: remove fixed collisions with contained surfaces
    def gen(obj_name, surface_name):
        obj_body = world.get_body(obj_name)
        surface_aabb = compute_surface_aabb(world, surface_name)
        learned_poses = load_placements(world, surface_name)
        while True:
            if learned:
                if not learned_poses:
                    break
                surface_pose_world = get_surface_reference_pose(world.kitchen, surface_name)
                sampled_pose_surface = multiply(surface_pose_world, random.choice(learned_poses))
                [x, y, _] = point_from_pose(sampled_pose_surface)
                _, _, yaw = euler_from_quat(quat_from_pose(sampled_pose_surface))
                dx, dy = np.random.normal(scale=pos_scale, size=2)
                z = stable_z_on_aabb(obj_body, surface_aabb)
                theta = wrap_angle(yaw + np.random.normal(scale=rot_scale))
                #yaw = np.random.uniform(*CIRCULAR_LIMITS)
                quat = quat_from_euler(Euler(yaw=theta))
                body_pose_world = (x+dx, y+dy, z+z_offset), quat
                # TODO: project onto the surface
            else:
                body_pose_world = sample_placement_on_aabb(obj_body, surface_aabb, epsilon=z_offset)
            if body_pose_world is None:
                break
            set_pose(obj_body, body_pose_world)
            if test_supported(world, obj_body, surface_name, collisions=collisions):
                surface = get_surface(surface_name)
                surface_link = link_from_name(world.kitchen, surface.link)
                attachment = create_attachment(world.kitchen, surface_link, obj_body)
                p = RelPose(obj_body, reference_body=world.kitchen,
                            reference_link=surface_link, support=surface_name, confs=[attachment])
                yield (p,)
    return gen


def get_grasp_gen(world, collisions=False, randomize=True, **kwargs): # teleport=False,
    def gen(name):
        for grasp in get_grasps(world, name, **kwargs):
            yield (grasp,)
    return gen

################################################################################

def inverse_reachability(world, base_generator, obstacles=[], max_attempts=25, **kwargs):
    lower_limits, upper_limits = get_custom_limits(
        world.robot, world.base_joints, world.custom_limits)
    while True:
        for i, base_conf in enumerate(islice(base_generator, max_attempts)):
            if not all_between(lower_limits, base_conf, upper_limits):
                continue
            #pose.assign()
            bq = Conf(world.robot, world.base_joints, base_conf)
            bq.assign()
            world.carry_conf.assign()
            if any(pairwise_collision(world.robot, b) for b in obstacles): #  + [obj]
                continue
            #print('IR attempts:', i)
            yield (bq,)
            break
        else:
            yield None

def compose_ir_ik(ir_sampler, ik_fn, inputs, max_attempts=25,
                  max_successes=1, max_failures=0, **kwargs):
    successes = 0
    failures = 0
    ir_generator = ir_sampler(*inputs)
    while True:
        for attempt in range(max_attempts):
            try:
                ir_outputs = next(ir_generator)
            except StopIteration:
                return
            if ir_outputs is None: # break instead?
                continue
            ik_outputs = next(ik_fn(*(inputs + ir_outputs)), None)
            if ik_outputs is None:
                continue
            successes += 1
            print('IK attempt:', attempt)
            yield ir_outputs + ik_outputs
            if max_successes < successes:
                return
            break
        else:
            failures += 1
            if max_failures < failures: # pose.init
                return
            yield None

################################################################################

def get_pick_ir_gen_fn(world, collisions=True, learned=True, **kwargs):
    # TODO: vary based on surface (for drawers)
    def gen_fn(name, pose, grasp):
        assert pose.support is not None
        obj = world.get_body(name)
        pose.assign() # May set the drawer confs as well
        obstacles = world.static_obstacles | get_surface_obstacles(world, pose.support)
        if not collisions:
            obstacles = set()
        for _ in iterate_approach_path(world, pose, grasp, body=obj):
            if any(pairwise_collision(world.gripper, b) or pairwise_collision(obj, b)
                   for b in obstacles):
                return iter([])

        # TODO: check collisions with obj at pose
        gripper_pose = multiply(pose.get_world_from_body(), invert(grasp.grasp_pose)) # w_f_g = w_f_o * (g_f_o)^-1
        if learned:
            base_generator = load_place_base_poses(world, gripper_pose, pose.support, grasp.grasp_type)
        else:
            base_generator = uniform_pose_generator(world.robot, gripper_pose)
        pose.assign()
        return inverse_reachability(world, base_generator, obstacles=obstacles, **kwargs)
    return gen_fn

ARM_RESOLUTION = 0.05
GRIPPER_RESOLUTION = 0.01
DOOR_RESOLUTION = 0.025

def plan_approach(world, approach_pose, obstacles=[], attachments=[],
                  teleport=False, switches_only=False, **kwargs):
    # TODO: use velocities in the distance function
    distance_fn = get_distance_fn(world.robot, world.arm_joints)
    aq = world.carry_conf
    grasp_conf = get_joint_positions(world.robot, world.arm_joints)
    if switches_only:
        return [aq.values, grasp_conf]

    full_approach_conf = world.solve_inverse_kinematics(approach_pose)
    if (full_approach_conf is None) or \
            any(pairwise_collision(world.robot, b) for b in obstacles): # TODO: | {obj}
        # print('Approach IK failure', approach_conf)
        return None
    approach_conf = get_joint_positions(world.robot, world.arm_joints)
    if teleport:
        return [aq.values, approach_conf, grasp_conf]
    if MAX_CONF_DISTANCE < distance_fn(grasp_conf, approach_conf):
        return None

    resolutions = ARM_RESOLUTION * np.ones(len(world.arm_joints))
    grasp_path = plan_direct_joint_motion(world.robot, world.arm_joints, grasp_conf,
                                          attachments=attachments,
                                          obstacles=obstacles, self_collisions=SELF_COLLISIONS,
                                          custom_limits=world.custom_limits, resolutions=resolutions / 4.)
    if grasp_path is None:
        print('Grasp path failure')
        return None
    aq.assign()
    # TODO: plan one with attachment placed and one held
    approach_path = plan_joint_motion(world.robot, world.arm_joints, approach_conf,
                                      attachments=attachments,
                                      obstacles=obstacles, self_collisions=SELF_COLLISIONS,
                                      custom_limits=world.custom_limits, resolutions=resolutions,
                                      restarts=2, iterations=25, smooth=25)
    if approach_path is None:
        print('Approach path failure')
        return None
    return approach_path + grasp_path

def plan_gripper_path(world, grasp_width, teleport=False, **kwargs):
    open_conf = [get_max_limit(world.robot, joint) for joint in world.gripper_joints]
    extend_fn = get_extend_fn(world.robot, world.gripper_joints,
                              resolutions=GRIPPER_RESOLUTION*np.ones(len(world.gripper_joints)))
    holding_conf = [grasp_width] * len(world.gripper_joints)
    if teleport:
        return [open_conf, holding_conf]
    return [open_conf] + list(extend_fn(open_conf, holding_conf))

def get_fixed_pick_gen_fn(world, randomize=False, collisions=True, **kwargs):
    sample_fn = get_sample_fn(world.robot, world.arm_joints)

    def gen(name, pose, grasp, base_conf):
        # TODO: check if within database convex hull
        # TODO: check approach
        # TODO: flag to check if initially in collision

        obj_body = world.get_body(name)
        world_from_body = pose.get_world_from_body()
        gripper_pose = multiply(world_from_body, invert(grasp.grasp_pose)) # w_f_g = w_f_o * (g_f_o)^-1
        approach_pose = multiply(world_from_body, invert(grasp.pregrasp_pose))
        gripper_attachment = grasp.get_attachment()

        surface = get_surface(pose.support)
        surface_link = link_from_name(world.kitchen, surface.link)
        finger_path = plan_gripper_path(world, grasp.grasp_width, **kwargs)
        obstacles = world.static_obstacles | get_surface_obstacles(world, pose.support) # | {obj_body}
        if not collisions:
            obstacles = set()

        pose.assign()
        base_conf.assign()
        world.open_gripper()
        robot_saver = BodySaver(world.robot)
        obj_saver = BodySaver(obj_body)

        aq = world.carry_conf
        if randomize:
            set_joint_positions(world.robot, world.arm_joints, sample_fn())
        else:
            aq.assign()
        full_grasp_conf = world.solve_inverse_kinematics(gripper_pose)
        if (full_grasp_conf is None) or any(pairwise_collision(world.robot, b) for b in obstacles):
            # print('Grasp IK failure', grasp_conf)
            return
        approach_path = plan_approach(world, approach_pose, obstacles=obstacles,
                                      attachments=[gripper_attachment], **kwargs)
        if approach_path is None:
            return

        surface_attachment = create_attachment(world.kitchen, surface_link, obj_body)
        cmd = Sequence(State(savers=[robot_saver, obj_saver], attachments=[surface_attachment]), commands=[
            Trajectory(world, world.robot, world.arm_joints, approach_path),
            Trajectory(world, world.robot, world.gripper_joints, finger_path),
            Detach(world, world.kitchen, surface_link, obj_body),
            Attach(world, world.robot, world.tool_link, obj_body),
            Trajectory(world, world.robot, world.arm_joints, reversed(approach_path)),
        ])
        #yield (aq, cmd,)
        yield (cmd,)
    return gen

def get_pick_gen_fn(world, max_attempts=25, teleport=False, **kwargs):
    # TODO: compose using general fn
    ir_sampler = get_pick_ir_gen_fn(world, max_attempts=1, **kwargs)
    ik_fn = get_fixed_pick_gen_fn(world, teleport=teleport, **kwargs)

    def gen(*inputs):
        _, pose, _ = inputs
        return compose_ir_ik(ir_sampler, ik_fn, inputs, max_attempts=max_attempts, **kwargs)
    return gen

################################################################################

def get_handle_grasp(world, joint, pre_distance=0.1):
    pre_direction = pre_distance * get_unit_vector([0, 0, 1])
    #half_extent = 0.375*FINGER_EXTENT[2]
    half_extent = 0.25*FINGER_EXTENT[2]

    for link in get_link_subtree(world.kitchen, joint):
        if 'handle' in get_link_name(world.kitchen, link):
            # TODO: can adjust the position and orientation on the handle
            handle_grasp = (Point(z=-half_extent), quat_from_euler(Euler(roll=np.pi, pitch=np.pi/2)))
            handle_pregrasp = multiply((pre_direction, unit_quat()), handle_grasp)
            return link, handle_grasp, handle_pregrasp
    raise RuntimeError()

def compute_door_path(world, joint_name, door_conf1, door_conf2, obstacles, teleport=False):
    if door_conf1 == door_conf2:
        return None
    door_joint = joint_from_name(world.kitchen, joint_name)
    door_joints = [door_joint]
    # TODO: could unify with grasp path
    door_extend_fn = get_extend_fn(world.kitchen, door_joints, resolutions=[DOOR_RESOLUTION])
    door_path = [door_conf1.values] + list(door_extend_fn(door_conf1.values, door_conf2.values))
    if teleport:
        door_path = [door_conf1.values, door_conf2.values]

    # door_obstacles = get_descendant_obstacles(world.kitchen, door_joint)
    handle_link, handle_grasp, handle_pregrasp = get_handle_grasp(world, door_joint)
    handle_path = []
    for door_conf in door_path:
        set_joint_positions(world.kitchen, door_joints, door_conf)
        # if any(pairwise_collision(door_obst, obst)
        #       for door_obst, obst in product(door_obstacles, obstacles)):
        #    return
        handle_path.append(get_link_pose(world.kitchen, handle_link))
        # Collide due to adjacency

    tool_path = [multiply(handle_pose, invert(handle_grasp)) for handle_pose in handle_path]
    for i, tool_pose in enumerate(tool_path):
        set_joint_positions(world.kitchen, door_joints, door_path[i])
        set_tool_pose(world, tool_pose)  # TODO: open gripper
        # handles = draw_pose(handle_path[i], length=0.25)
        # handles.extend(draw_aabb(get_aabb(world.kitchen, link=handle_link)))
        # wait_for_user()
        # for handle in handles:
        #    remove_debug(handle)
        if any(pairwise_collision(world.gripper, obst) for obst in obstacles):
            return None
    return door_path, handle_path, tool_path

def plan_pull(world, door_joint, door_path, handle_path, tool_path, base_conf,
              randomize=True, collisions=True, teleport=False, **kwargs):
    handle_link, handle_grasp, handle_pregrasp = get_handle_grasp(world, door_joint)
    door_joints = [door_joint]
    obstacles = world.static_obstacles | get_descendant_obstacles(world.kitchen, door_joint)
    if not collisions:
        obstacles = set()
    # TODO: could allow handle collisions
    # TODO: could push if the goal is to fully close
    # TODO: check door/bq collisions

    base_conf.assign()
    world.open_gripper()
    #door_saver = BodySaver()
    robot_saver = BodySaver(world.robot)
    sample_fn = get_sample_fn(world.robot, world.arm_joints)
    distance_fn = get_distance_fn(world.robot, world.arm_joints)
    aq = world.carry_conf
    if randomize:
        set_joint_positions(world.robot, world.arm_joints, sample_fn())
    else:
        aq.assign()

    arm_path = []
    for i, tool_pose in enumerate(tool_path):
        set_joint_positions(world.kitchen, door_joints, door_path[i])
        full_arm_conf = world.solve_inverse_kinematics(tool_pose)
        # TODO: only check moving links
        if (full_arm_conf is None) or any(pairwise_collision(world.robot, b) for b in obstacles):
            # print('Approach IK failure', approach_conf)
            return
        arm_conf = get_joint_positions(world.robot, world.arm_joints)
        if arm_path and not teleport:
            if MAX_CONF_DISTANCE < distance_fn(arm_path[-1], arm_conf):
                return
        arm_path.append(arm_conf)
        # wait_for_user()

    approach_paths = []
    for index in [0, -1]:
        set_joint_positions(world.kitchen, door_joints, door_path[index])
        set_joint_positions(world.robot, world.arm_joints, arm_path[index])
        tool_pose = multiply(handle_path[index], invert(handle_pregrasp))
        approach_path = plan_approach(world, tool_pose, obstacles=obstacles,
                                      teleport=teleport, **kwargs)
        if approach_path is None:
            return
        approach_paths.append(approach_path)

    set_joint_positions(world.kitchen, door_joints, door_path[0])
    set_joint_positions(world.robot, world.arm_joints, arm_path[0])
    grasp_width = close_until_collision(world.robot, world.gripper_joints,
                                        bodies=[(world.kitchen, [handle_link])])
    finger_path = plan_gripper_path(world, grasp_width, teleport=teleport)

    cmd = Sequence(State(savers=[robot_saver]), commands=[
        Trajectory(world, world.robot, world.arm_joints, approach_paths[0]),
        Trajectory(world, world.robot, world.gripper_joints, finger_path),
        DoorTrajectory(world, world.robot, world.arm_joints, arm_path,
                       world.kitchen, door_joints, door_path),
        Trajectory(world, world.robot, world.gripper_joints, reversed(finger_path)),
        Trajectory(world, world.robot, world.arm_joints, reversed(approach_paths[-1])),
    ])
    #yield (aq, cmd,)
    yield (cmd,)

################################################################################

def get_fixed_pull_gen_fn(world, collisions=True, teleport=False, **kwargs):
    obstacles = world.static_obstacles
    if not collisions:
        obstacles = set()

    def gen(joint_name, door_conf1, door_conf2, base_conf):
        # TODO: check if within database convex hull
        door_joint = joint_from_name(world.kitchen, joint_name)
        result = compute_door_path(world, joint_name, door_conf1, door_conf2, obstacles, teleport=teleport)
        if result is None:
            return
        door_path, handle_path, tool_path = result
        return plan_pull(world, door_joint, door_path, handle_path, tool_path, base_conf,
                         collisions=collisions, teleport=teleport, **kwargs)
    return gen

def get_pull_gen_fn(world, collisions=True, teleport=False, learned=True, **kwargs):
    # TODO: could condition pick/place into cabinet on the joint angle
    obstacles = world.static_obstacles
    if not collisions:
        obstacles = set()

    def gen(joint_name, door_conf1, door_conf2):
        door_joint = joint_from_name(world.kitchen, joint_name)
        result = compute_door_path(world, joint_name, door_conf1, door_conf2, obstacles, teleport=teleport)
        if result is None:
            return
        door_path, handle_path, tool_path = result
        index = int(len(tool_path)/2) # index = 0
        target_pose = tool_path[index]
        if learned:
            base_generator = load_pull_base_poses(world, joint_name)
        else:
            base_generator = uniform_pose_generator(world.robot, target_pose)

        for ir_outputs in inverse_reachability(world, base_generator, obstacles=obstacles):
            if ir_outputs is None: # break instead?
                yield None
                continue
            base_conf, = ir_outputs
            ik_outputs = next(plan_pull(world, door_joint, door_path, handle_path, tool_path, base_conf,
                                        collisions=collisions, teleport=teleport, **kwargs), None)
            if ik_outputs is None:
                continue
            yield ir_outputs + ik_outputs
    return gen

################################################################################

def parse_fluents(world, fluents, obstacles):
    attachments = []
    for fluent in fluents:
        predicate, args = fluent[0], fluent[1:]
        if predicate == 'AtBConf'.lower():
            bq, = args
            bq.assign()
        elif predicate == 'AtAConf'.lower():
            aq, = args
            aq.assign()
        elif predicate == 'AtAngle'.lower():
            raise RuntimeError()
            # j, a = args
            # a.assign()
            # obstacles.update(get_descendant_obstacles(a.body, a.joints[0]))
        elif predicate in {p.lower() for p in ['AtPose', 'AtWorldPose']}:
            b, p = args
            p.assign()
            obstacles.update(get_link_obstacles(world, b))
        elif predicate == 'AtGrasp'.lower():
            b, g = args
            attachments.append(g.get_attachment())
            attachments[-1].assign()
        else:
            raise NotImplementedError(predicate)
    return attachments

def get_base_motion_fn(world, collisions=True, teleport=False):
    # TODO: ensure only forward drive?

    def fn(bq1, bq2, fluents=[]):
        bq1.assign()
        world.carry_conf.assign()
        obstacles = set(world.static_obstacles)
        attachments = parse_fluents(world, fluents, obstacles)
        # TODO: could condition on arm conf
        if not collisions:
            obstacles = set()
        initial_saver = BodySaver(world.robot)
        if teleport:
            path = [bq1.values, bq2.values]
        else:
            path = plan_nonholonomic_motion(world.robot, bq2.joints, bq2.values, attachments=attachments,
                                            obstacles=obstacles, custom_limits=world.custom_limits,
                                            self_collisions=False,
                                            restarts=4, iterations=75, smooth=100)
            if path is None:
                print('Failed to find a base motion plan!')
                #for bq in [bq1, bq2]:
                #    bq.assign()
                #    wait_for_user()
                return None
        # TODO: could actually plan with all joints as long as we return to the same config
        cmd = Sequence(State(savers=[initial_saver]), commands=[
            Trajectory(world, world.robot, world.base_joints, path),
        ])
        return (cmd,)
    return fn

def get_arm_motion_gen(world, collisions=True, teleport=False):
    resolutions = ARM_RESOLUTION * np.ones(len(world.arm_joints))

    def fn(aq1, aq2, fluents=[]):
        # TODO: condition explicitly on a base conf?
        aq1.assign()
        obstacles = set(world.static_obstacles)
        attachments = parse_fluents(world, fluents, obstacles)
        if not collisions:
            obstacles = set()
        initial_saver = BodySaver(world.robot)
        if teleport:
            path = [aq1.values, aq2.values]
        else:
            path = plan_joint_motion(world.robot, aq2.joints, aq2.values,
                                     attachments=attachments,
                                     obstacles=obstacles, self_collisions=SELF_COLLISIONS,
                                     custom_limits=world.custom_limits, resolutions=resolutions,
                                     restarts=2, iterations=25, smooth=25)
            if path is None:
                print('Failed to find an arm motion plan!')
                return None
        cmd = Sequence(State(savers=[initial_saver]), commands=[
            Trajectory(world, world.robot, world.arm_joints, path),
        ])
        return (cmd,)
    return fn

################################################################################

def get_calibrate_gen(world, collisions=True, teleport=False):

    def fn(bq):
        # TODO: include if holding anything?
        bq.assign()
        aq = world.carry_conf
        aq.assign()
        world.open_gripper()
        robot_saver = BodySaver(world.robot)
        cmd = Sequence(State(savers=[robot_saver]), commands=[
            #Trajectory(world, world.robot, world.arm_joints, approach_path),
            # TODO: calibrate command
        ])
        #return (aq, cmd,)
        return (cmd,)
    return fn

################################################################################

OPEN = 'open'
CLOSED = 'closed'
DOOR_STATUSES = [OPEN, CLOSED]
JOINT_THRESHOLD = 1e-3
# TODO: retrieve from entity

def get_door_test(world):
    def test(joint_name, conf, status):
        [joint] = conf.joints
        [position] = conf.values
        if status == OPEN:
            status_position = world.open_conf(joint)
        elif status == CLOSED:
            status_position = world.closed_conf(joint)
        else:
            raise NotImplementedError(status)
        return abs(position - status_position) <= JOINT_THRESHOLD
    return test

################################################################################

def get_cfree_pose_pose_test(world, collisions=True, **kwargs):
    def test(o1, rp1, o2, rp2, s):
        if not collisions or (o1 == o2):
            return True
        rp1.assign()
        rp2.assign()
        return not pairwise_collision(world.get_body(o1), world.get_body(o2))
    return test

def get_cfree_approach_pose_test(world, collisions=True, **kwargs):
    def test(o1, p1, g1, o2, p2):
        if not collisions or (o1 == o2):
            return True
        body = world.get_body(o1)
        p2.assign()
        obstacles = get_link_obstacles(world, o2) # - {body}
        for _ in iterate_approach_path(world, p1, g1, body=body):
            if any(pairwise_collision(part, obst) for part in
                   [world.gripper, body] for obst in obstacles):
                return False
        return True
    return test

################################################################################

def check_collision_free(world, state, sequence, obstacles):
    if not obstacles:
        return True
    # TODO: check door collisions
    state.assign()
    for command in sequence.commands:
        for _ in command.iterate(world, state):
            state.derive()
            for attachment in state.attachments.values():
                if any(pairwise_collision(attachment.child, obst) for obst in obstacles):
                    return False
            if any(pairwise_collision(world.robot, obst) for obst in obstacles):
                return False
    # TODO: just check collisions with moving links
    return True

def get_cfree_traj_pose_test(world, collisions=True, **kwargs):
    def test(at, o, p):
        if not collisions:
            return True
        # TODO: do per individual trajectory
        p.assign()
        obstacles = get_link_obstacles(world, o) - at.bodies
        state = copy.copy(at.context)
        return check_collision_free(world, state, at, obstacles)
    return test

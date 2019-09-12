from __future__ import print_function

import numpy as np
import random
import time

from pybullet_tools.utils import set_pose, Pose, Point, Euler, multiply, get_pose, \
    create_box, set_all_static, COLOR_FROM_NAME, \
    stable_z_on_aabb, pairwise_collision, elapsed_time, get_aabb_extent, get_aabb, create_cylinder
from src.stream import get_stable_gen, MAX_COST
from src.utils import JOINT_TEMPLATE, BLOCK_SIZES, BLOCK_COLORS, COUNTERS, \
    ALL_JOINTS, LEFT_CAMERA, CAMERA_MATRIX, CAMERA_POSES, CAMERAS, compute_surface_aabb, \
    BLOCK_TEMPLATE, name_from_type, GRASP_TYPES, SIDE_GRASP, joint_from_name, STOVES
from examples.discrete_belief.dist import UniformDist, DeltaDist
#from examples.pybullet.pr2_belief.problems import BeliefState, BeliefTask, OTHER
from src.belief import create_surface_belief

class Task(object):
    def __init__(self, world, prior={}, skeletons=[], grasp_types=GRASP_TYPES,
                 movable_base=True, noisy_base=True, teleport_base=False,
                 return_init_bq=False, return_init_aq=False, goal_aq=None,
                 init_liquid=[], goal_liquid=[],
                 goal_hand_empty=False, goal_holding=None, goal_detected=[],
                 goal_on={}, goal_open=[], goal_closed=[], goal_cooked=[],
                 init=[], goal=[], max_cost=MAX_COST):
        self.world = world
        world.task = self
        self.prior = dict(prior) # DiscreteDist over
        self.skeletons = list(skeletons)
        self.grasp_types = tuple(grasp_types)
        self.movable_base = movable_base
        self.noisy_base = noisy_base
        self.teleport_base = teleport_base
        assert (goal_aq is None) or not return_init_aq
        self.goal_aq = goal_aq
        self.return_init_bq = return_init_bq
        self.return_init_aq = return_init_aq
        self.init_liquid = init_liquid
        self.goal_liquid = goal_liquid
        self.goal_hand_empty = goal_hand_empty
        self.goal_holding = goal_holding
        self.goal_on = dict(goal_on)
        self.goal_detected = set(goal_detected)
        self.goal_open = set(goal_open)
        self.goal_closed = set(goal_closed)
        self.goal_cooked = set(goal_cooked)
        self.init = init
        self.goal = goal
        self.max_cost = max_cost # TODO: use instead of the default
    @property
    def objects(self):
        return sorted(set(self.prior.keys()))
    def create_belief(self):
        t0 = time.time()
        print('Creating initial belief')
        belief = create_surface_belief(self.world, self.prior)
        belief.task = self
        print('Took {:2f} seconds'.format(elapsed_time(t0)))
        return belief
    def __repr__(self):
        return '{}{}'.format(self.__class__.__name__, {
            key: value for key, value in self.__dict__.items() if value not in [self.world]})

################################################################################

# (x, y, yaw)
UNIT_POSE2D = (0., 0., 0.)
BOX_POSE2D = (0.1, 1.05, 0.) # 1.15
SPAM_POSE2D = (0.125, 1.175, -np.pi / 4)
CRACKER_POSE2D = (0.2, 1.1, np.pi/4) # 1.2
BIG_BLOCK_SIDE = 0.065

def add_block(world, idx=0, **kwargs):
    # TODO: automatically produce a unique name
    color = 'green'
    block_type = '{}_block'.format(color)
    #block_type = BLOCK_TEMPLATE.format(BLOCK_SIZES[-1], BLOCK_COLORS[0])
    #block_type = 'potted_meat_can'
    name = name_from_type(block_type, idx)
    #world.add_body(name)
    #print(get_aabb_extent(get_aabb(world.get_body(name))))
    side = BIG_BLOCK_SIDE
    body = create_box(w=side, l=side, h=side, color=COLOR_FROM_NAME[color])
    world.add(name, body)
    pose2d_on_surface(world, name, COUNTERS[0], **kwargs)
    return name

def add_ycb(world, ycb_type, idx=0, **kwargs):
    name = name_from_type(ycb_type, idx)
    world.add_body(name, color=np.ones(4))
    pose2d_on_surface(world, name, COUNTERS[0], **kwargs)
    return name

add_sugar_box = lambda world, **kwargs: add_ycb(world, 'sugar_box', **kwargs)
add_cracker_box = lambda world, **kwargs: add_ycb(world, 'cracker_box', **kwargs)

def add_box(world, color_name, idx=0, **kwargs):
    name = name_from_type(color_name, idx)
    # TODO: geometry type
    body = create_box(w=0.07, l=0.07, h=0.14, color=COLOR_FROM_NAME[color_name])
    world.add(name, body)
    # pose2d_on_surface(world, name, COUNTERS[0], **kwargs)
    return name

def add_cylinder(world, color_name, idx=0, **kwargs):
    name = name_from_type(color_name, idx)
    body = create_cylinder(radius=0.07/2, height=0.14, color=COLOR_FROM_NAME[color_name])
    world.add(name, body)
    # pose2d_on_surface(world, name, COUNTERS[0], **kwargs)
    return name

def add_kinect(world, camera_name=LEFT_CAMERA):
    # TODO: could intersect convex with half plane
    world_from_camera = multiply(get_pose(world.kitchen), CAMERA_POSES[camera_name])
    world.add_camera(camera_name, world_from_camera, CAMERA_MATRIX)

################################################################################

BASE_POSE2D = (0.74, 0.80, -np.pi)

def set_fixed_base(world):
    # set_base_values(world.robot, BASE_POSE2D)
    world.set_base_conf(BASE_POSE2D)

def pose2d_on_surface(world, entity_name, surface_name, pose2d=UNIT_POSE2D):
    x, y, yaw = pose2d
    body = world.get_body(entity_name)
    surface_aabb = compute_surface_aabb(world, surface_name)
    z = stable_z_on_aabb(body, surface_aabb)
    pose = Pose(Point(x, y, z), Euler(yaw=yaw))
    set_pose(body, pose)
    return pose

def sample_placement(world, entity_name, surface_name, **kwargs):
    entity_body = world.get_body(entity_name)
    placement_gen = get_stable_gen(world, pos_scale=1e-3, rot_scale=1e-2, **kwargs)
    for pose, in placement_gen(entity_name, surface_name):
        pose.assign()
        if not any(pairwise_collision(entity_body, obst_body) for obst_body in
                   world.body_from_name.values() if entity_body != obst_body):
            return pose
    raise RuntimeError('Unable to find a pose for object {} on surface {}'.format(entity_name, surface_name))

def close_all_doors(world):
    for joint in world.kitchen_joints:
        world.close_door(joint)

def open_all_doors(world):
    for joint in world.kitchen_joints:
        world.open_door(joint)

################################################################################

def detect_block(world, fixed=False, **kwargs):
    for side in CAMERAS[:1]:
        add_kinect(world, side)
    if fixed:
        set_fixed_base(world)

    entity_name = add_block(world, idx=0, pose2d=BOX_POSE2D)
    #x, y, yaw = CRACKER_POSE2D
    sugar_name = add_sugar_box(world, idx=0, pose2d=CRACKER_POSE2D)
    #cracker_name = add_cracker_box(world, idx=0, pose2d=(x, 1.4, yaw))
    #other_name = add_box(world, idx=1)
    set_all_static()

    goal_surface = 'indigo_drawer_top'
    initial_distribution = UniformDist([goal_surface]) # indigo_tmp
    initial_surface = initial_distribution.sample()
    if random.random() < 0.:
        # TODO: sometimes base/arm failure causes the planner to freeze
        # Freezing is because the planner is struggling to find new samples
        sample_placement(world, entity_name, initial_surface, learned=True)
    #sample_placement(world, other_name, 'hitman_tmp', learned=True)

    prior = {
        entity_name: UniformDist(['indigo_tmp']),  # 'indigo_tmp', 'indigo_drawer_top'
        sugar_name: DeltaDist('indigo_tmp'),
        #cracker_name: DeltaDist('indigo_tmp'),
    }
    return Task(world, prior=prior, movable_base=not fixed,
                return_init_bq=True, return_init_aq=True,
                #goal_detected=[entity_name],
                goal_holding=entity_name,
                #goal_on={entity_name: goal_surface},
                goal_closed=ALL_JOINTS,
            )

################################################################################

def hold_block(world, num=5, fixed=False, **kwargs):
    add_kinect(world)
    if fixed:
        set_fixed_base(world)

    # TODO: compare with the NN grasp prediction in clutter
    # TODO: consider a task where most directions are blocked except for one
    initial_surface = 'indigo_tmp'
    # initial_surface = 'dagger_door_left'
    # joint_name = JOINT_TEMPLATE.format(initial_surface)
    #world.open_door(joint_from_name(world.kitchen, joint_name))
    #open_all_doors(world)

    prior = {}
    # green_name = add_block(world, idx=0, pose2d=BOX_POSE2D)
    green_name = add_box(world, 'green', idx=0)
    prior[green_name] = DeltaDist(initial_surface)
    sample_placement(world, green_name, initial_surface, learned=True)
    for idx in range(num):
        red_name = add_box(world, 'red', idx=idx)
        prior[red_name] = DeltaDist(initial_surface)
        sample_placement(world, red_name, initial_surface, learned=True)

    set_all_static()

    return Task(world, prior=prior, movable_base=not fixed,
                # grasp_types=GRASP_TYPES,
                grasp_types=[SIDE_GRASP],
                return_init_bq=True, return_init_aq=True,
                goal_holding=green_name,
                #goal_closed=ALL_JOINTS,
            )


################################################################################

def sugar_drawer(world, fixed=False, **kwargs):
    add_kinect(world)
    if fixed:
        set_fixed_base(world)

    initial_surface = 'indigo_drawer_top' # indigo_drawer_top | indigo_drawer_bottom
    #initial_surface = 'indigo_tmp'
    joint_name = JOINT_TEMPLATE.format(initial_surface)
    world.open_door(joint_from_name(world.kitchen, joint_name))
    # open_all_doors(world)
    # TODO: approach for pull
    prior = {}
    block_name = add_block(world, idx=0, pose2d=BOX_POSE2D)
    prior[block_name] = DeltaDist('indigo_tmp')

    cracker_name = add_sugar_box(world, idx=0)
    prior[cracker_name] = DeltaDist(initial_surface)
    sample_placement(world, cracker_name, initial_surface, learned=True)

    set_all_static()

    return Task(world, prior=prior, movable_base=not fixed,
                goal_on={block_name: initial_surface},
                return_init_bq=True, return_init_aq=True,
                #goal_open=[JOINT_TEMPLATE.format('indigo_drawer_top')],
                goal_closed=ALL_JOINTS,
            )

################################################################################

def cook_block(world, fixed=True, **kwargs):
    add_kinect(world) # previously needed to be after set_all_static?
    if fixed:
        set_fixed_base(world)

    entity_name = add_block(world, idx=0, pose2d=BOX_POSE2D)
    set_all_static()

    initial_surface = 'indigo_tmp'
    sample_placement(world, entity_name, initial_surface, learned=True)

    prior = {
        entity_name: UniformDist([initial_surface]),
    }
    return Task(world, prior=prior, movable_base=not fixed,
                #goal_detected=[entity_name],
                goal_holding=entity_name,
                goal_cooked=[entity_name],
                #goal_on={entity_name: goal_surface},
                return_init_bq=True, return_init_aq=True,
                #goal_open=[joint_name],
                #goal_closed=ALL_JOINTS,
            )

################################################################################

def detect_drawers(world, fixed=True, **kwargs):
    add_kinect(world) # previously needed to be after set_all_static?
    if fixed:
        set_fixed_base(world)

    # set_base_values
    entity_name = add_block(world, idx=0, pose2d=BOX_POSE2D)
    set_all_static()

    drawers = ['indigo_drawer_top', 'indigo_drawer_bottom']
    #initial_surface, goal_surface = 'indigo_tmp', 'indigo_drawer_top'
    #initial_surface, goal_surface = 'indigo_drawer_top', 'indigo_drawer_top'
    #initial_surface, goal_surface = 'indigo_drawer_bottom', 'indigo_drawer_bottom'
    initial_surface, goal_surface = drawers
    #initial_surface, goal_surface = reversed(drawers)
    if initial_surface != 'indigo_tmp':
        sample_placement(world, entity_name, initial_surface, learned=True)

    #joint_name = JOINT_TEMPLATE.format(goal_surface)
    #world.open_door(joint_from_name(world.kitchen, JOINT_TEMPLATE.format(goal_surface)))

    # TODO: declare success if already believe it's in the drawer or require detection?
    prior = {
        #entity_name: UniformDist([initial_surface]),
        entity_name: UniformDist(drawers),
        #entity_name: UniformDist(['indigo_tmp', 'indigo_drawer_top', 'indigo_drawer_bottom']),
    }
    return Task(world, prior=prior, movable_base=not fixed,
                #goal_detected=[entity_name],
                #goal_holding=entity_name,
                #goal_cooked=[entity_name],
                goal_on={entity_name: goal_surface},
                return_init_bq=True, return_init_aq=True,
                #goal_open=[joint_name],
                goal_closed=ALL_JOINTS,
            )

################################################################################

def stow_block(world, num=1, fixed=False, **kwargs):
    add_kinect(world) # previously needed to be after set_all_static?
    if fixed:
        set_fixed_base(world)

    # initial_surface = random.choice(DRAWERS) # COUNTERS | DRAWERS | SURFACES | CABINETS
    initial_surface = 'indigo_tmp'  # hitman_tmp | indigo_tmp | range | front_right_stove
    # initial_surface = 'indigo_drawer_top'
    goal_surface = 'indigo_drawer_top'  # baker | hitman_drawer_top | indigo_drawer_top | hitman_tmp | indigo_tmp
    print('Initial surface: | Goal surface: ', initial_surface, initial_surface)

    prior = {}
    goal_on = {}
    for idx in range(num):
        #entity_name = add_block(world, idx=idx, pose2d=SPAM_POSE2D)
        entity_name = add_ycb(world, 'tomato_soup_can', idx=idx, pose2d=SPAM_POSE2D) # mustard_bottle | tomato_soup_can
        prior[entity_name] = DeltaDist(initial_surface)
        goal_on[entity_name] = goal_surface
        if not fixed:
            sample_placement(world, entity_name, initial_surface, learned=True)

    stove = STOVES[-1]
    bowl_name = add_ycb(world, 'bowl')
    prior[bowl_name] = DeltaDist(stove)
    sample_placement(world, bowl_name, stove, learned=True)

    #obstruction_name = add_box(world, idx=0)
    #sample_placement(world, obstruction_name, 'hitman_tmp')
    set_all_static()

    #joint_name = 'indigo_drawer_top_joint'
    #world.open_door(joint_from_name(world.kitchen, joint_name))

    return Task(world, prior=prior, movable_base=not fixed,
                init_liquid=[(entity_name, 'food')],
                goal_liquid=[(bowl_name, 'food')],
                goal_holding=list(prior)[0],
                #goal_on=goal_on,
                #goal_cooked=list(prior),
                return_init_bq=True, return_init_aq=True,
                #goal_open=[joint_name],
                goal_closed=ALL_JOINTS
            )

################################################################################

TASKS = [
    detect_block,
    hold_block,
    detect_drawers,
    sugar_drawer,
    cook_block,
    stow_block,
]

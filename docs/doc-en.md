# CodeCraft 2020: the game

The rules of the game, its entities and the client API, as the organisers of the 2020 championship published them:
the one document the clients and the engine follow (the lines about the 2020 schedule are gone). How this server
runs the game, with its ladder, ratings, leagues and limits, is written in
[The arena](https://codecraft.day/rules#the-arena).

## About testing and strategy limits

Time in the game is discrete and is measured in "ticks". At the beginning of each tick, the game simulator transmits the world state data to the participants' strategies, then receives actions from them and updates the state of the world in accordance with these actions and the rules of the game. Then the process is repeated again for next tick with the updated state. There is a maximum duration of the game, but it can also be terminated prematurely if all strategies have "crashed".

The "crashed" strategy can no longer control player's behavior. The strategy is considered "crashed" in the following cases:

- The process in which the strategy is started has unexpectedly terminated, or an error has occurred in the protocol of interaction between the strategy and game server.

- The strategy exceeded one of the time constraints assigned to it. There is a time limit for strategy to reply with an action for each tick - `1` second of real time, as well as a time limit for the whole game - `40` seconds of cpu time.

- The strategy exceeded the memory limit - `256` MB.

## Game overview

The game of CodeCraft 2020 is a strategy where you will have to control a number of units, gather resources, build your settlement and attack your enemies.

Your goal is to gain more score than your opponents. The game ends either when the max tick count has been reached or when there is only one (or zero) players left.
When playing 1v1 (Finals), when only one player is left, he gets additional score enough to win the game.

The game is played on a rectangular grid, divided into tiles. All game entities have square shape and are located at some integer coordinates. When calculating distance, we count the number of tiles that need to be traversed to reach destination, going to a neighboring tile at one time (Manhattan distance).

Entities' behavior is defined by their properties.

One of the most important properties defines the size of an entity. All entities in the game have square shape, with a side length equal to the defined value.

Some entities can move (these entities are called units). Moving entities always have a size of `1`. They can move to a neighboring tile in one tick, if that tile is not occupied by some other entity.

Some entities can attack, and all entities have health and can be destroyed. If entity's health gets below or equal to zero, it is removed from the game. Attacking entities have a limited attacking range, which is a maximum distance to the target for performing the attack. Each tick an entity is attacking, it subtracts a certain amount of health points from the target.

Also, some entities can repair other entities. Only adjacent entities can be repaired. Repairing is restoring a specified amount of health points in on tick. When repairing, target's health can not go above its maximum health, specified in its properties. Only alive entities (with positive health) can be repaired.

Some of the attacking entities can also collect resources from the target. For each health point of damage, a fixed amount of resource (specified in target entity's properties) is added to the attacker's owning player.

Gathered resources can be used to build new entities. Some entity types can do that. New entity's type is limited by capabilities of the builder, listed in its properties. To build a new entity, you have to spend a specified amount of resources. For units (movable entities), the exact amount of resources is equal to the value specified in properties of this unit's type, plus current amount of player's units of this type. For other entities, the cost is equal to the initial cost. You should also select a location not occupied by other entities, and located adjacent to the builder. Newly built entities have initial health equal either to entity's maximum health, or to a specific value, as specified in builder entity's properties.

When an entity is just built, it is inactive at first, meaning it can not perform any actions. To activate an entity, it has to reach its maximum health first. So, if an entity was built not with full health, it needs to be repaired first.

Also, there is one more restriction to building new entities. Besides resources, there is another parameter called "population". Some entities provide population, while others use it. In order to build a new entity, the sum of population provided by all current player's active entities should be greater than or equal to the sum of population used by all current player's entities, including newly built one.

The last property of an entity is its sight range. If fog of war is enabled, your strategy can only see those entities that are located no further than distance specified from some entity that is controlled by you.

## List of entity types

There is a fixed number of entity types in the game, and entities of same type have same properties. Here is a full list of entity types:

- Resource. This is the only entity type that is not controlled by any player. It has a size of `1` and should be attacked by builder units in order to be gathered.

- Builder unit. Its primary purpose is to gather resources and construct buildings.

- Melee unit. Basic damage dealing unit with an attack range of `1`.

- Ranged unit. Deals less damage than melee unit, but has bigger attack range.

- Builder/Melee/Ranged bases. These are buildings used to buy new units of corresponding type. Can be built by a builder unit.

- Wall. A small building that can be used to block path for enemy.

- House. A building that provides population.

- Turret. A building that can attack enemies. Since it can not move, its best for defence.

## Control interface

Each tick your strategy is going to be asked for actions of your entities. If you do not specify action for an entity, it continues to perform previously set action.

Entity's action consists of attack, build, repair and move actions, which are prioritized in this order. So, if you specify multiple actions, then only the first one that is possible to do this current tick is actually performed.

Attack action can specify a specific entity to attack, or perform an auto attack. When using auto attack, you can also specify a distance where your unit should try to pathfind to try and find an enemy to attack.

Repair action can only specify an entity to repair.

For build action you would need to specify a type of entity you want to build as well as position. Position of an entity is it's corner with minimal coordinates.

For move action target position needs to be specified. A unit will try to pathfind to this position. You can further control pathfinding by specifying whether to find closest position. In case you do not want this, unit will not move if the path is not found. You may also specify whether to try to find such a route which involves breaking through other entities, attacking and destroying them on the way. Such algorithm will only consider to attack entities not controlled by you.

When pathfinding is needed to be performed by the game server, a simple A* algorithm is used, with limited number of nodes to be visited.

Each game tick, first pathfinding is being performed for moving entities to determine their potential next position.
If entity's move action target is adjacent to the entity, no pathfinding is performed, and instead this position is being remembered.
Next, all attack actions are performed for active entities in random order. If no valid target is found, but the position found in previous step contains an enemy, this enemy is being attacked. If target's health was positive and becomes zero, target is considered destroyed and score is added to the attacker (but target is not removed from the game just yet).
Next, build actions are performed (if an entity has performed an attack action this tick, build action will not be performed).
Then repair actions are performed in same way. Only entities with positive health are being repaired.
And last, movement is being performed. Movement is performed in steps.
In each step, entities are trying to move to their next position as determined in the pathfinding stage.
If several entities are trying to move to the same location, a random one is chosen.
If no entities can be moved, movement phase is finished.

In the end, entities with zero health are removed from the game, and entities with full health become active.

## Round specific rules

Three game formats are played. The server draws one for every rated game; a custom game is set up in any of them.

**Round 1**: four players, no fog of war, and a base for each unit type given at the start, so a strategy can gather
resources and attack right away. Experimenting with buildings here prepares for the other formats.

**Round 2**: four players, fog of war, and only builders at the start: the bases for the other unit types have to be
built, and the enemy found before it can be fought.

**Finals**: one on one, with fog of war and builders only at the start, as in Round 2. If only one player is left
in the game, they are given enough score to win it.

## Testing on your machine

The local runner plays the server's games on your computer (the same engine, rules and maps), live in the browser,
with the debug interface described below: your strategy draws on the map and logs, and reads the mouse and the keys.
The [quick start](https://codecraft.day/quickstart) has the runner and the commands of every language.

## Entity properties values

Here you can see the value of entity properties:

```
{
    House: (
        size: 3,
        build_score: 50,
        destroy_score: 500,
        can_move: false,
        population_provide: 5,
        population_use: 0,
        max_health: 50,
        initial_cost: 50,
        sight_range: 5,
        resource_per_health: 0,
        build: None,
        attack: None,
        repair: None,
    ),
    RangedUnit: (
        size: 1,
        build_score: 30,
        destroy_score: 300,
        can_move: true,
        population_provide: 0,
        population_use: 1,
        max_health: 10,
        initial_cost: 30,
        sight_range: 10,
        resource_per_health: 0,
        build: None,
        attack: Some((
            range: 5,
            damage: 5,
            collect_resource: false,
        )),
        repair: None,
    ),
    BuilderUnit: (
        size: 1,
        build_score: 10,
        destroy_score: 100,
        can_move: true,
        population_provide: 0,
        population_use: 1,
        max_health: 10,
        initial_cost: 10,
        sight_range: 10,
        resource_per_health: 0,
        build: Some((
            options: [
                House,
                Wall,
                BuilderBase,
                MeleeBase,
                RangedBase,
                Turret,
            ],
            init_health: Some(5),
        )),
        attack: Some((
            range: 1,
            damage: 1,
            collect_resource: true,
        )),
        repair: Some((
            valid_targets: [
                House,
                Wall,
                BuilderUnit,
                MeleeUnit,
                RangedUnit,
                BuilderBase,
                MeleeBase,
                RangedBase,
                Turret,
            ],
            power: 1,
        )),
    ),
    MeleeUnit: (
        size: 1,
        build_score: 20,
        destroy_score: 200,
        can_move: true,
        population_provide: 0,
        population_use: 1,
        max_health: 50,
        initial_cost: 20,
        sight_range: 10,
        resource_per_health: 0,
        build: None,
        attack: Some((
            range: 1,
            damage: 5,
            collect_resource: false,
        )),
        repair: None,
    ),
    Wall: (
        size: 1,
        build_score: 10,
        destroy_score: 10,
        can_move: false,
        population_provide: 0,
        population_use: 0,
        max_health: 50,
        initial_cost: 10,
        sight_range: 2,
        resource_per_health: 0,
        build: None,
        attack: None,
        repair: None,
    ),
    Resource: (
        size: 1,
        build_score: 0,
        destroy_score: 0,
        can_move: false,
        population_provide: 0,
        population_use: 0,
        max_health: 30,
        initial_cost: 0,
        sight_range: 0,
        resource_per_health: 1,
        build: None,
        attack: None,
        repair: None,
    ),
    Turret: (
        size: 2,
        build_score: 50,
        destroy_score: 500,
        can_move: false,
        population_provide: 0,
        population_use: 0,
        max_health: 100,
        initial_cost: 50,
        sight_range: 10,
        resource_per_health: 0,
        build: None,
        attack: Some((
            range: 5,
            damage: 5,
            collect_resource: false,
        )),
        repair: None,
    ),
    BuilderBase: (
        size: 5,
        build_score: 500,
        destroy_score: 5000,
        can_move: false,
        population_provide: 5,
        population_use: 0,
        max_health: 300,
        initial_cost: 500,
        sight_range: 5,
        resource_per_health: 0,
        build: Some((
            options: [
                BuilderUnit,
            ],
            init_health: None,
        )),
        attack: None,
        repair: None,
    ),
    RangedBase: (
        size: 5,
        build_score: 500,
        destroy_score: 5000,
        can_move: false,
        population_provide: 5,
        population_use: 0,
        max_health: 300,
        initial_cost: 500,
        sight_range: 5,
        resource_per_health: 0,
        build: Some((
            options: [
                RangedUnit,
            ],
            init_health: None,
        )),
        attack: None,
        repair: None,
    ),
    MeleeBase: (
        size: 5,
        build_score: 500,
        destroy_score: 5000,
        can_move: false,
        population_provide: 5,
        population_use: 0,
        max_health: 300,
        initial_cost: 500,
        sight_range: 5,
        resource_per_health: 0,
        build: Some((
            options: [
                MeleeUnit,
            ],
            init_health: None,
        )),
        attack: None,
        repair: None,
    ),
}

```

## API description

In language pack for your programming language you can find file named `MyStrategy.<ext>`/`my_strategy.<ext>`.
This file contains class `MyStrategy` with `get_action` method, where your strategy's logic should be implemented.

This method will be called each tick.

The method takes following arguments:

- Player view — all the information you have about current game's state,
- Debug interface — this object allows you to do send debug commands to the local runner and receive debug state from inside your strategy code. Note that this is unavailable when testing your strategy on the server, or using the app in batch mode. This is for local debugging only.

The method should return the action you desire to perform this tick.

For debugging purposes, there is also another method — `debug_update`, that has same parameters, and is called continiously while the local runner is running (not in batch mode), if the client is waiting for the next tick. There will always be at least one debug update between ticks.

## Objects description

In this section, some fields may be absent (denoted as `Option<type>`).
The way this is implemented depends on the language used.
If possible, a dedicated optional (nullable) type would be used,
otherwise other methods may be used (like a nullable pointer type).

Some objects may take one of several forms. The way it is implemented depends on the language.
If possible, a dedicated sum (algebraic) data type is used,
otherwise other methods may be used (like variants being classes inherited from abstract base class).

### `Vec2Float32`

2 dimensional vector.

Fields:

- `x`: `float32` - `x` coordinate of the vector
- `y`: `float32` - `y` coordinate of the vector

### `Color`

RGBA Color

Fields:

- `r`: `float32` - Red component
- `g`: `float32` - Green component
- `b`: `float32` - Blue component
- `a`: `float32` - Alpha (opacity) component

### `ColoredVertex`

Vertex for debug rendering

Fields:

- `world_pos`: `Option<Vec2Float32>` - Position in world coordinates (if none, screen position (0, 0) is used)
- `screen_offset`: `Vec2Float32` - Additional offset in screen coordinates
- `color`: `Color` - Color to use

### `PrimitiveType`

Primitive type for debug rendering

Variants:

- `Lines` - Lines, number of vertices should be divisible by 2
- `Triangles` - Triangles, number of vertices should be divisible by 3

### `DebugData`

Debug data can be drawn in the local runner

One of:

- `Log` - Log some text

    Fields:

    - `text`: `string` - Text to show
- `Primitives` - Draw primitives

    Fields:

    - `vertices`: `[ColoredVertex]` - Vertices
    - `primitive_type`: `PrimitiveType` - Primitive type
- `PlacedText` - Draw text

    Fields:

    - `vertex`: `ColoredVertex` - Vertex to determine text position and color
    - `text`: `string` - Text
    - `alignment`: `float32` - Text alignment (0 means left, 0.5 means center, 1 means right)
    - `size`: `float32` - Font size in pixels

### `DebugCommand`

Debug commands that can be sent while debugging with the local runner

One of:

- `Add` - Add debug data to current tick

    Fields:

    - `data`: `DebugData` - Data to add
- `Clear` - Clear current tick's debug data

    No fields

- `SetAutoFlush` - Enable/disable auto performing of commands

    Fields:

    - `enable`: `boolean` - Enable/disable autoflush
- `Flush` - Perform all previously sent commands

    No fields


### `Vec2Int32`

2 dimensional vector.

Fields:

- `x`: `int32` - `x` coordinate of the vector
- `y`: `int32` - `y` coordinate of the vector

### `MoveAction`

Move action

Fields:

- `target`: `Vec2Int32` - Target position
- `find_closest_position`: `boolean` - Whether to try find closest position, if path to target is not found
- `break_through`: `boolean` - Whether to destroy other entities on the way

### `EntityType`

Entity type

Variants:

- `Wall` - Wall, can be used to prevent enemy from moving through
- `House` - House, used to increase population
- `BuilderBase` - Base for recruiting new builder units
- `BuilderUnit` - Builder unit can build buildings
- `MeleeBase` - Base for recruiting new melee units
- `MeleeUnit` - Melee unit
- `RangedBase` - Base for recruiting new ranged units
- `RangedUnit` - Ranged unit
- `Resource` - Resource can be harvested
- `Turret` - Ranged attacking building

### `BuildAction`

Build action

Fields:

- `entity_type`: `EntityType` - Type of an entity to build
- `position`: `Vec2Int32` - Desired position of new entity

### `AutoAttack`

Auto attack options

Fields:

- `pathfind_range`: `int32` - Maximum distance to pathfind
- `valid_targets`: `[EntityType]` - List of target entity types to try to attack. If empty, all types but resource are considered

### `AttackAction`

Attack action

Fields:

- `target`: `Option<int32>` - If specified, target entity's ID
- `auto_attack`: `Option<AutoAttack>` - If specified, configures auto attacking

### `RepairAction`

Repair action

Fields:

- `target`: `int32` - Target entity's ID

### `EntityAction`

Entity's action

Fields:

- `move_action`: `Option<MoveAction>` - Move action
- `build_action`: `Option<BuildAction>` - Build action
- `attack_action`: `Option<AttackAction>` - Attack action
- `repair_action`: `Option<RepairAction>` - Repair action

### `Action`

Player's action

Fields:

- `entity_actions`: `Map<int32 -> EntityAction>` - New actions for entities. If entity does not get new action, if will continue to perform previously set one

### `ClientMessage`

Message sent from client

One of:

- `DebugMessage` - Ask app to perform new debug command

    Fields:

    - `command`: `DebugCommand` - Command to perform
- `ActionMessage` - Reply for ServerMessage::GetAction

    Fields:

    - `action`: `Action` - Player's action
- `DebugUpdateDone` - Signifies finish of the debug update

    No fields

- `RequestDebugState` - Request debug state from the local runner

    No fields


### `BuildProperties`

Entity's build properties

Fields:

- `options`: `[EntityType]` - Valid new entity types
- `init_health`: `Option<int32>` - Initial health of new entity. If absent, it will have full health

### `AttackProperties`

Entity's attack properties

Fields:

- `attack_range`: `int32` - Maximum attack range
- `damage`: `int32` - Damage dealt in one tick
- `collect_resource`: `boolean` - If true, dealing damage will collect resource from target

### `RepairProperties`

Entity's repair properties

Fields:

- `valid_targets`: `[EntityType]` - Valid target entity types
- `power`: `int32` - Health restored in one tick

### `EntityProperties`

Entity properties

Fields:

- `size`: `int32` - Size. Entity has a form of a square with side of this length
- `build_score`: `int32` - Score for building this entity
- `destroy_score`: `int32` - Score for destroying this entity
- `can_move`: `boolean` - Whether this entity can move
- `population_provide`: `int32` - Number of population points this entity provides, if active
- `population_use`: `int32` - Number of population points this entity uses
- `max_health`: `int32` - Maximum health points
- `initial_cost`: `int32` - Cost to build this first entity of this type. If this is a unit (entity can move), the cost is increased by 1 for each existing unit of this type
- `sight_range`: `int32` - If fog of war is enabled, maximum distance at which other entities are considered visible
- `resource_per_health`: `int32` - Amount of resource added to enemy able to collect resource on dealing damage for 1 health point
- `build`: `Option<BuildProperties>` - Build properties, if entity can build
- `attack`: `Option<AttackProperties>` - Attack properties, if entity can attack
- `repair`: `Option<RepairProperties>` - Repair properties, if entity can repair

### `Player`

Player (strategy, client)

Fields:

- `id`: `int32` - Player's ID
- `score`: `int32` - Current score
- `resource`: `int32` - Current amount of resource

### `Entity`

Game entity

Fields:

- `id`: `int32` - Entity's ID. Unique for each entity
- `player_id`: `Option<int32>` - Entity's owner player ID, if owned by a player
- `entity_type`: `EntityType` - Entity's type
- `position`: `Vec2Int32` - Entity's position (corner with minimal coordinates)
- `health`: `int32` - Current health
- `active`: `boolean` - If entity is active, it can perform actions

### `PlayerView`

Information available to the player

Fields:

- `my_id`: `int32` - Your player's ID
- `map_size`: `int32` - Size of the map
- `fog_of_war`: `boolean` - Whether fog of war is enabled
- `entity_properties`: `Map<EntityType -> EntityProperties>` - Entity properties for each entity type
- `max_tick_count`: `int32` - Max tick count for the game
- `max_pathfind_nodes`: `int32` - Max pathfind nodes when performing pathfinding in the game simulator
- `current_tick`: `int32` - Current tick
- `players`: `[Player]` - List of players
- `entities`: `[Entity]` - List of entities

### `ServerMessage`

Message sent from server

One of:

- `GetAction` - Get action for next tick

    Fields:

    - `player_view`: `PlayerView` - Player's view
    - `debug_available`: `boolean` - Whether app is running with debug interface available
- `Finish` - Signifies end of the game

    No fields

- `DebugUpdate` - Debug update

    Fields:

    - `player_view`: `PlayerView` - Player's view

### `Camera`

Camera used for rendering

Fields:

- `center`: `Vec2Float32` - Center point at which camera is looking
- `rotation`: `float32` - Rotation angle
- `attack`: `float32` - Attack angle
- `distance`: `float32` - Distance to center
- `perspective`: `boolean` - Whether perspective is applied

### `DebugState`

Debug state to be received from the local runner

Fields:

- `window_size`: `Vec2Int32` - Size of the drawing canvas
- `mouse_pos_window`: `Vec2Float32` - Mouse position in window coordinates
- `mouse_pos_world`: `Vec2Float32` - Mouse position in world coordinates
- `pressed_keys`: `[string]` - Currently pressed keys
- `camera`: `Camera` - Current camera used for rendering
- `player_index`: `int32` - Your player's index

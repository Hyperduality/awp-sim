# Changelog

## 0.1.0a6

Targets specification revision `0.1-draft.9`.

- Under the barrier, a pending `world.tick` is lost with its connection. The barrier no longer advances on it while its session is suspended.
- A holder that transfers one of its embodiments loses that embodiment's action types and channel grants with it.
- A resumed session keeps its command channel grant; resumption dropped it.
- `awp-sim manifest` reports an unknown or unsupported feature as an error instead of a traceback.
- A replay bundle from awp-sim 0.1.0a4 or earlier loads again, with the gripper fully open.

## 0.1.0a5

Targets specification revision `0.1-draft.9`.

- `--features gripper` adds a second embodiment, `gripper_01`. One session can bind it with the arm, or several sessions can share it. In lockstep, it makes the tick authority `barrier`.
- An observer session closing in lockstep no longer halts the arm.
- The reports come from awp-conformance 0.1.0a5, and the feature configurations include the gripper.

## 0.1.0a4

Targets specification revision `0.1-draft.9`.

- This is the first release as a separate package. Through 0.1.0a3, `awp_sim` shipped inside awp-python, whose [changelog](https://github.com/Hyperduality/awp-python/blob/main/CHANGELOG.md) covers it. awp-sim now depends on awp-python for the protocol layer.
- The demo agent moves to awp-python as `awp-demo`. The `awp-sim demo` subcommand is gone.
- CI runs awp-conformance against each configuration the reports claim.

# Changelog

## 0.1.0a4

Targets specification revision `0.1-draft.9`.

- This is the first release as a separate package. Through 0.1.0a3, `awp_sim` shipped inside awp-python, whose [changelog](https://github.com/Hyperduality/awp-python/blob/main/CHANGELOG.md) covers it. awp-sim now depends on awp-python for the protocol layer.
- The demo agent moves to awp-python as `awp-demo`. The `awp-sim demo` subcommand is gone.
- CI runs awp-conformance against each configuration the reports claim.

# AGENTS.md

awp-sim is the reference world for the Agent World Protocol, published on PyPI as `awp-sim`. It serves a simulated arm, and optionally a gripper, in either time model. The engine (`src/awp_sim/world.py`) is a sans-IO state machine, and the server wraps it in WebSockets. It speaks AWP through awp-python's protocol layer and targets the revision pinned as the `spec/` submodule.

## Checks

```bash
git submodule update --init
uv sync
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest --cov
uv run awp-sim scenarios --out traces && uv run python scripts/check_traces.py traces/*.jsonl   # needs Node 20+
```

CI runs these checks. It also runs awp-conformance against the four configurations in `conformance/README.md`, and fails unless each claim is AWP-conformant.

## Code

- Protocol behavior belongs in the engine. The server adds only transport, timers, and the audit log.
- Cite a requirement ID (`AWP-XXX-NNN`) where the engine implements it: the engine is meant to be read alongside the specification. Otherwise, comment only what the code can't say.
- In development, `awp-python` comes from its git `main` (`[tool.uv.sources]`). A release depends on the published version named in `dependencies`.

## Conformance

`conformance/` holds the reports behind the README's claim, plus the evidence for their `manual` rows.

- The reports must come from the suite version CI runs.
- CI fetches its fixtures from that suite version's tag.
- After bumping the suite version in `.github/workflows/ci.yml`, regenerate the four reports with the commands in `conformance/README.md`, then update the versions named there and in the README.

## Commits and pull requests

- Branch from `main` and open a pull request. Merge once CI passes.
- Write the title as one plain sentence in sentence case, with no trailing period, saying what changed: `Run awp-conformance 0.1.0a4 in CI`. When the change is part of a release, end it with the version: `(0.1.0a4)`.
- Add a body only when the title can't carry the reason: one or two short sentences.
- Write commits the way a person on the project would. No `Co-Authored-By` trailers, no "Generated with" lines, and no other mention of AI tools, in commits or in PRs.
- The PR title matches the commit title, and the description is a few lines at most.

## Moving to a new draft revision

1. Release an awp-python that targets the new revision.
2. Check out the revision's tag (`spec-v0.1-draft.N`) in `spec/`.
3. Raise the `awp-python` requirement in `pyproject.toml`.
4. Fix whatever the tests report.

## Releasing

1. In a pull request:
   - Set the version with `uv version <version>`, for example `0.1.0a5`.
   - Add a `CHANGELOG.md` entry that starts "Targets specification revision `0.1-draft.N`." and lists the user-visible changes.
2. Once it is merged, tag the merge commit and push the tag:

   ```bash
   git tag -a v0.1.0a5 -m "awp-sim 0.1.0a5 (AWP 0.1-draft.N)"
   git push origin v0.1.0a5
   ```

3. `release.yml` checks that the tag matches the version and builds the package. It publishes through PyPI trusted publishing once the `pypi` environment is approved.
   - A maintainer gives that approval. Agents never approve deployments.
   - Agents never publish with a token.

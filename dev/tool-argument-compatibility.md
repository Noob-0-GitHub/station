# Optional tool fields: omission is not an empty value

Developer note; not an AI-facing tool manual. Audit baseline: `858d120`.

**Development status:** the action-scoped fixes below are implemented in the development tree, not deployed. Historical findings describe the baseline; the explicit `changes` contract avoids guessing whether relevant empty values mean omission or a deliberate reset.

## The observed failure

The Codex fallback repeatedly requested sleep using this payload:

```json
{"action":"sleep","minutes":0,"once":false,"expr":"","note":"","hook":"","id":0}
```

The previous schedule handler required the request keys to equal `{"action"}`. It returned an error for every attempt, despite the intended operation being clear. The result was a repeated model/tool-error loop, not a missing tool response. The deployed sleep-only fix tolerates empty values for irrelevant fields and still rejects fields carrying real alarm settings.

This observation establishes what the model returned in that incident. It does **not** establish why it returned every field, nor that every Codex call behaves this way.

## Provider boundary

`Codex.build` in `station/llm.py` passes each shared `input_schema` as the Responses function's `parameters`. It does not rewrite `required`, add `strict`, or insert optional argument values. `from_responses_output` JSON-decodes the returned arguments without removing optional fields.

Do not claim that Station itself forces all fields to be required, or that upstream strict-schema normalization has been demonstrated. Testing an explicit `strict: false` against the subscription endpoint is a separate, opt-in live experiment; it has not been done by this audit.

## Confirmed hazards in other tools

### Focus: key-presence checks reject empty unrelated fields

- `new` rejects `id: ""`, because the presence of `id` is forbidden even though the value is empty.
- `model` and `close` reject `hide_boards: [], hide_tags: []`: `board_view.parse_update` returns an update dictionary, which the action guard treats as an attempted visibility change.
- `view` rejects even empty model/context fields because the handler checks whether the keys exist.
- After omitting visibility fields, `model` with `provider: "", model: ""` still fails nonempty-provider validation instead of merely inspecting the current route.
- `switch` also reaches `Router.configure`; empty provider/model fields are not equivalent to omitted overrides.

References: `Life.focus_tool`, `Router.configure`, `board_view.parse_update`.

### Board visibility: empty selectors still count as selectors

- `board hide` with `name: "plans", tag: ""` fails the exactly-one-selector check.
- `board show` with `all: true, name: "", tag: ""` fails because those empty selectors are still counted as present.

Reference: `board_view.change`, whose selector test is `args.get(key) is not None`.

These are reproducible handler behaviors with constructed placeholder payloads, not evidence that production has already hit every case.

### Skill: an empty file argument hides the supporting-file index

`Skills.read(name, file)` selects `file or "SKILL.md"`, but adds the supporting-file index only when `file is None`. A schema-valid `file: ""` therefore reads the main skill and silently omits its index. `null` behaves like omission. Other falsy wrong-type values (`0`, `false`, `[]`, `{}`) also omit the index, though those values do not satisfy the declared string schema. Six new offline cases reproduce these behaviors with a temporary local skill.

Reference: `station/skills.py:45-56`. This is information loss, not a file-read failure.

## Remaining tool review

The combined review covers all **13 tool definitions**: 11 life tools and 2 onboarding tools. The independent pass covered the 11 tools other than Focus/Schedule, comprising 26 action-or-call entry points and 23 top-level optional fields; those counts were checked programmatically against `station/tools.py`. Static inspection is not an exhaustive runtime or live-provider test.

- `diary`: optional empty dates fall back to today; list ignores text/date and write ignores date. Empty required entry text is correctly rejected.
- `todo`: unrelated text/id arguments are ignored per action. A missing/invalid id on done/drop is a business-input error, not an irrelevant-field conflict.
- `memory`: empty search limit falls back to 8; empty kind/since act as no filter. Other actions ignore unrelated search fields. Missing query, note text or id is still an input error.
- `web`: search ignores urls and defaults empty max_results to 5; extract ignores query/max_results. Empty required query/urls are correctly rejected.
- `bash`: empty timeout falls back to the configured default. No optional-field key-presence hazard was found in this path.
- `compress` and `exit`: empty focus/reason become empty strings.
- `view` and `register`: no optional properties exist; an empty path/name is a required-input issue.
- Non-visibility `board` actions ignore unrelated fields. Empty write text and empty patch replacement are valid operations, not disposable placeholders.

These findings do not promise acceptance of arbitrary malformed types. The audit considers omitted or placeholder optional fields with the relevant action's required inputs supplied; it does not legitimize a missing target, empty required body, or invalid path.

## Empty values that must retain their meaning

A global `if value` filter is not a fix:

- `schedule interval` with `minutes: 0` disables the interval.
- `once: false` means a recurring alarm, not a missing boolean.
- `focus fallback: []` explicitly disables fallback; omission inherits or preserves configuration.
- `focus hide_boards: []` and `hide_tags: []` clear visibility rules; omission preserves them.
- `effort: null` is explicitly accepted and can replace the previous effort override.
- `board patch new: ""` can intentionally delete the matching text; an empty write can be intentional too.

A placeholder cannot always be distinguished from an intentional clear by its value alone. In particular, blindly accepting every empty field can turn a harmless inspection into a state change. `focus model` with only `fallback: []` legitimately installs a no-fallback override. Empty visibility lists legitimately clear hidden panels. Preserve these contracts unless an explicit API change is approved.

## Development rules

1. Separate field omission, explicit null, and intentional empty/zero/false values in the contract.
2. Review relevance per action before changing validation; do not copy the sleep-only compatibility rule into every tool.
3. Normalize only fields proven irrelevant to the chosen action. Keep required-field errors and real conflicts visible.
4. If a field has meaningful empty semantics, remove ambiguity through the API contract or an explicit operation, not a heuristic that silently guesses intent.
5. Validate the complete request before mutating thread, model, visibility, schedule, or file state.
6. Add offline tests for minimal arguments, schema-filled irrelevant placeholders, intentional clear/disable values, malformed required inputs, and nonempty conflicting fields.
7. Treat repeated identical tool errors as a separate loop-resilience problem. Schema compatibility alone is not a retry limit, and no new automatic retry limit is introduced by this audit.
8. Keep shared tool schemas unless there is a tested reason to diverge. Any provider-specific transport option needs endpoint evidence, not an assumption borrowed from another API.

## Development fix

Anthony asked to return to development and explicitly skipped the source-edit dry run. No production deployment is included.

- `station/tool_args.py` normalizes a copy of Focus arguments before state mutation. It removes only empty irrelevant or explicitly unselected setting fields, using a field-specific whitelist: strings accept empty strings/null, lists accept []/null, and inherit accepts false/null. Wrong-type falsy values such as provider 0, hide_tags {}, or inherit 0 are not swallowed. Selected values still go through normal validation. It rejects nonempty conflicts, unsupported/missing mask fields, duplicate field names, invalid selected values and custom new-thread ids.
- The shared Focus schema adds optional `changes`, an array of setting names or null. Present null/[] selects no setting updates; selecting fallback or visibility fields preserves the intentional [] clear/disable contract. The mask does not suppress new/switch/close or change title/goal/id/context. Existing sparse calls without a mask retain relevant-field semantics. An empty provider/model pair bundled with effort/fallback is rejected with instructions instead of silently becoming a reset.
- `board_view.change` treats an unused empty string selector as absent and an optional null all flag as absent. Real dual selectors, nonempty conflicts and nonboolean flags are still errors.
- `Skills.read` uses the same falsy main-file test for appending its file index as it already used for choosing SKILL.md. Explicit nonempty file reads retain their old behavior.
- AI-facing usage examples and exact mask semantics are in `docs/threads.md`. No Codex-specific schema fork, strict flag, model retry limit or production configuration change is added.

Thirteen behavior-adjusted audit cases passed (the unchanged null Skill case and other unchanged passing cases were excluded). Forty-four additional update-mask, no-mutation, type/conflict and intentional-clear regression cases passed in a separate run. Independent review then prompted a narrower, field-specific placeholder whitelist; 60 further cases passed, covering accepted placeholders versus wrong-type falsy values, mixed meaningful/empty model bundles, invalid actions, and preservation of disk state, pending switches, failover state and notices on failure. Total incremental fix coverage: 117 passing cases across these runs. Existing passing tests and the full suite were not rerun. Python syntax and JSON examples were validated separately. This is offline evidence only, not a live-provider compatibility claim.

The compatibility boundary is deliberate: legacy calls with a valid provider/model plus fallback [] still disable fallback, and legacy view calls with visibility [] still clear those rules. New inspection calls should use changes []. No heuristic can safely distinguish those legacy clear operations from model-generated placeholders. Skills continues its previous falsy main-file selection behavior; appending the index consistently is not a new type-validation guarantee.

## Offline audit evidence and scope

At the baseline, `tests/test_tool_placeholder_audit.py` characterized the hazards rather than fixing them. Behavior-adjusted cases now assert the development fixes, while unaffected audit cases remain. The original expected exceptions were evidence of compatibility hazards; a passing characterization test did not mean those hazards were repaired.

The first run had eight passing cases and one failed expectation: the full `focus model` payload is rejected by the visibility guard *before* provider validation. That expectation was corrected, and a separate provider-validation case was added. Only the failed case and the new case were then run; both passed. Ten distinct characterization cases now have passing results across those runs. Previously successful cases and the wider suite were not rerun. Tests ran with sockets blocked and a separate network namespace; no real model calls were made.

The remaining source review found the additional Skill index issue above. Eight new cases (six Skill placeholder values, shared-schema forwarding, and empty-value decode/replay) passed in a separate run. In total, 18 distinct new characterization cases have passing results across the incremental runs; no full-suite pass is claimed. Tool schemas and transport conversion were exercised offline, not against a live endpoint.

This document and the characterization tests do not change production code, service state, credentials, schemas, or runtime history.

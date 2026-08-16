# DeepSeek V4 API constraints

Verified live against `deepseek-v4-flash` on 2026-08-11. These are not preferences —
they are the shape the API actually accepts, and the scoring module depends on all three.

## 1. `response_format: json_schema` does not exist yet

```
400  This response_format type is unavailable now
```

So LangChain's `with_structured_output(..., method="json_schema")` **cannot work against
DeepSeek**, with or without `strict=True`. Use `method="function_calling"` — but see the
next section, and read section 3 before you trust the schema.

`response_format: {"type": "json_object"}` *is* supported (plain JSON mode, no schema
enforcement). That is the fallback if function calling ever regresses.

## 2. Forced `tool_choice` requires thinking mode OFF

V4 models think by default. A forced tool choice — which is exactly what strict structured
output compiles to — is rejected while thinking is on:

```
400  Thinking mode does not support this tool_choice
```

Two levers turn thinking off; both were verified to return 200 with a forced tool choice:

| lever | how | notes |
|---|---|---|
| `reasoning_effort="none"` | first-class constructor arg on `ChatDeepSeek` | **preferred** — no `model_kwargs` warning |
| `thinking={"type":"disabled"}` | via `extra_body` | works, but LangChain warns about passing it through `model_kwargs` |

Rejected spellings (still 400): `reasoning_effort="minimal"`, `enable_thinking=false`.

**Consequence for the two agents:**

- **scoring** needs a forced tool call, so it disables thinking
  (`reasoning_effort="none"`) and uses `method="function_calling", strict=True`.
- **research** wants thinking on, so it must use `tool_choice="auto"` — which *is*
  accepted in thinking mode — rather than forcing a tool.

## 3. `strict=True` does not validate the arguments

This one cost 38 postings their verdict in a single backlog run, so it is worth stating
plainly. A forced `tool_choice` guarantees the model **calls** the tool. It does not
guarantee the arguments match the schema, and DeepSeek does not check them.

Measured over one run of the scoring agent (386 rejected calls, 2026-08-14/15):

| what the schema said | what arrived |
|---|---|
| `Literal["met", "partial", "unmet"]` | `"blocked"`, 126 times |
| 8 required fields | absent, the object ended early, 37 times |
| a property named `requirement` | a property named `question`, 46 times |

So every constraint in a Pydantic model is a **post-hoc** check here, paid for with a
completed call. Design accordingly:

- Put the allowed values in the field's `description`. The model reads the description;
  it evidently does not read the enum.
- Repair in a `mode="before"` validator whatever has an obvious reading. A raise costs a
  whole retry, and at `temperature=0` with thinking off the retry is the same draw at the
  same conditions — the enum leak above cleared on only 41% of second attempts.
- Reserve a raise for cases where judgement is genuinely missing rather than mistyped.

## 4. Prefix caching is automatic, and worth designing around

No headers, no opt-in. Repeated prompt prefixes bill at the cache-hit rate:

| | $/MTok |
|---|---|
| input, cache hit | $0.0028 |
| input, cache miss | $0.14 |
| output | $0.28 |

Measured hits land on 64-token boundaries (768, 1408, 1664, 1920 observed), so the cached
span is quantised — a prefix a few tokens shy of a boundary loses that block.

**This makes prompt order architectural.** Build every scoring prompt as:

```
[ stable prefix ]   system rules + profiles.summary_text      <- cached
[ volatile suffix ] <posting> ... </posting>                   <- billed at full rate
```

Never interpolate the posting title, a job id, or a timestamp into the prefix — it
invalidates the cache for every request and silently multiplies input cost by 50x.

**Regression check:** score two postings back to back and assert
`prompt_cache_hit_tokens > 0` on the second. Zero across consecutive calls means something
is leaking into the prefix.

## 5. Temperature is unset by default, and that is not free

`ChatDeepSeek` sends no `temperature` unless you pass one, so the API samples at its
default of 1.0. Measured by scoring the same 12 postings twice through the identical
prompt (2026-08-11):

| | mean abs. difference | max | identical |
|---|---|---|---|
| temperature unset (1.0) | 5.7 points | 15 | 5/12 |
| `temperature=0` | 0.0 points | 0 | 12/12 |

Fit bands are 20 points wide, so a ~6-point spread makes the band a posting lands in
partly a draw, and makes re-scoring a corpus produce churn that reads as a changed
opinion. `structured_model()` therefore pins `temperature=0`; callers who want sampling
pass it explicitly.

Two consequences worth keeping in mind when reading a distribution:

- **Scores were quantised, which is why the model no longer emits one.** Across 5,511
  verdicts it used 53 distinct values with strong attractors (5, 8, 12, 15, 18, 22, 25, 30,
  35, 45, 55, 62, 72, 78, 82) and never emitted 79, 80 or 81, so `strong` was reached by
  jumping 78 -> 82. It now answers five ordinals and `scoring/scale.py` computes the number.
  See the README.
- **An empty top band is not necessarily prompt suppression.** Adding an explicit
  calibration note about the top band moved the highest-scoring group by +1.1 points,
  well inside the noise it was competing with. Fix the sampling before rewriting a rubric.

**Temperature 0 is not full determinism.** The 12/12 figure above is a small sample. Re-running
`evals/metamorphic.py` over the same 10 postings moves one or two ordinals between runs, so
treat repeated identical output as likely rather than guaranteed.

Check any of this with `careerradar score stats`.

## 6. Measured token shape (for cost estimates)

Against real postings from `jobs.db` with a ~700-token profile prefix:

| | tokens |
|---|---|
| prompt, total | 1,460 – 2,016 |
| prompt, cached (warm prefix) | ~768 (the system+profile block) |
| completion | 562 – 670 |

Completion runs ~600 tokens, not the ~300 originally assumed — verdicts carry quoted
blockers and reasoning. Budget **~$0.0004/posting**, i.e. roughly **$2.30** to score the
current 5,932-row backlog.

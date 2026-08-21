# Streaming & the chunkable-driver contract

`Tracked<T>` journals grow with op count: a long characterization run can
buffer tens of gigabytes if the journal is only written at the end. This
document is the library-owned contract for **chunked** drivers — writing the
journal incrementally with bounded memory while keeping every downstream
guarantee (id stability, sample grouping, reduce/merge equality) intact.

## The API

```cpp
std::ofstream out("journal.jsonl");
for (chunk : chunks) {
    run_chunk(chunk);                        // Tracked ops accumulate records
    tracked::journal::flush_and_clear(out);  // write + drop them, keep counters
}
```

`journal::flush_and_clear(std::ostream&)`:

- writes the calling thread's buffered records and **clears the buffer** (and
  the lazy lookup caches), bounding memory to one chunk;
- does **NOT** reset the callsite id counters — the id sequence of a chunked
  run is bit-identical to the monolithic run's (`journal::clear()`, by
  contrast, resets counters and is a *run boundary*);
- writes the v1 schema header when the stream is at position 0. On a
  non-seekable sink it writes a header per call, which is still conforming: a
  mid-stream header is a shard-boundary record readers validate and skip
  (docs/SCHEMA.md).

`journal::flush(path)` (whole-buffer, truncating) remains for small runs.

## The chunkable-driver contract

Two chunking axes, with different strength guarantees:

- **Within one process** (`flush_and_clear` between chunks): the concatenated
  stream is **byte-identical** to the monolithic run's journal — counters are
  never reset, so ids continue exactly.
- **Across processes** (`--sample-offset` / `--sample-count` shard reruns):
  callsite counters restart per process, so a shard's ids carry shard-local
  `#<counter>` suffixes. The guarantee is *reduction equivalence*, not byte
  equality with the monolithic file: every grouping key the tools use (the
  scope suffix, the `line=`/`at` region key, source-variable names) is
  shard-invariant, ids are only correlated *within* one sample batch, and a
  given shard invocation is bit-reproducible run-to-run (which is what a
  one-sample viewer drill-down needs).

The library guarantees its half (id counters, scope suffixes, header
handling); the driver must supply:

1. **Pure per-sample inputs.** Each sample's inputs must be a function of the
   *global* sample index (a per-unit RNG reseed, or closed-form generation) —
   never of how many samples ran before it in this process.
2. **Prefix refill with recordless leaves.** If a driver must fast-forward an
   RNG to reach `--sample-offset`, it may generate and discard prefix values
   freely as raw scalars, or wrap them with the leaf factories — `track()`,
   `constant()`, and `literal()` emit **no journal records**. (Note `literal()`
   and the `Tracked(T)` ctor do advance the `_lit` id counter; that only
   affects `_lit#<n>` suffixes, which nothing groups on.)
3. **Scope every sample.** Wrap each unit and sample in scopes
   (`integral=<unit>` / `sample=<global index>`), so ids are globally unique
   across chunks, threads, and shards. Scope values must satisfy the v1 scope
   grammar (validated on push).
4. **Sample contiguity per stream.** Emit each `(unit, sample)` fully before
   the next (RAII scopes + serial per-thread execution give this for free).
   Reducers group per-sample batches by *contiguous* runs of the sample key.
5. **One journal stream per thread.** Never interleave two threads' records
   into one stream — contiguity dies.

Under 1–5, `merge([reduce(chunk_1), …, reduce(chunk_n)]) ==
reduce(chunk_1 ++ … ++ chunk_n)` — the reduce/merge associativity the sharded
characterizer depends on (see docs/SCHEMA.md for the shard-disjointness
precondition).

## Threading guidance

Everything mutable in the library is `thread_local`: the record buffer, the
callsite id counters, the scope stack, and the lookup caches. There are no
data races and no cross-thread visibility — which cuts both ways:

- **Scopes pushed on the main thread are invisible to workers.** Push the
  `integral=`/`sample=` scopes on the thread that runs the ops.
- **Id uniqueness across threads comes from the scope suffix alone.** Two
  threads running the same call sites produce the same
  `<op>@<file>:<line>#<n>` prefixes; only the `@…/sample=<i>` suffix
  disambiguates them. Therefore: **never dispatch the same (unit, sample) on
  two threads**, and never run unscoped work on more than one thread.
- Each thread flushes its own buffer to its own stream
  (`flush_and_clear(per_thread_out)`); shard files combine by reduce+merge or
  by concatenation under the SCHEMA.md precondition.

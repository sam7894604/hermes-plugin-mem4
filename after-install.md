# mem4 installed — next steps

mem4 is an **opt-in** memory provider. Installing it changes nothing until you
both **enable** the plugin and point Hermes' memory provider at it.

1. Enable the plugin:

   ```
   hermes plugins enable mem4
   ```

2. Turn it on in `config.yaml`:

   ```yaml
   memory:
     provider: mem4
     mem4:
       backend: local-file   # default; the only backend in the minimal chassis
       dream:
         enabled: true        # event-triggered consolidation (default on)
   ```

3. (Optional) build the recall index from any microfiles you already have:

   ```
   hermes mem4 rebuild
   ```

## Safety notes

- mem4 runs in **coexist/augment** mode. It **only ever reads** the built-in
  `MEMORY.md`/`USER.md` and mirrors writes into its own files under
  `$HERMES_HOME/mem4/`. It never writes, moves, or deletes the built-in memory.
- To disable: remove `memory.provider: mem4` from `config.yaml`. Hermes falls
  back to pure built-in memory with **zero residue** — the `$HERMES_HOME/mem4/`
  directory is inert and can be deleted at any time.
- The default `local-file` backend has **no external dependencies** and makes
  **no network/MCP calls** on the turn hot path.

See the [README](./README.md) for the full design, tools, and measurement harness.

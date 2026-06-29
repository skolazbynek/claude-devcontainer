This is a master design document for to-do features and fixes that should be completed.

1: In the same way as `cld chain` can specify persona using `@<persona>` notation, `cld agent` should be able to accept the same form. In case of current dir structure:
```
prompts
|
--- example_prompt.txt
--- personas
      |
      --- persona.txt
```
then for:
- `@prompt.txt`
the command should look through the whole `prompts` directory including `personas` subdir and select the one that matches. If the command cannot identify a single file (there are duplicates in subfolders), error and write out all the duplicates. Otherwise behave the exact same way as for `cld chain`.

2: Master CLD:
    User should be able to run a devcontainer, exit out of it, and then return to the same one later (on the same git repo). There should be an option in the `cld devcontainer` command that doesn't remove the container on it's exit but keeps it running, and similarly a command that allows a user to return automatically to it's running devcontainer specific to the project repo he's currently in. Only single long-running devcontainer per repo. Additionally include shutdown subcommands to kill a specific master devcontainer or all master devcontainers across all projects.
Example UX: (naming can be different later)
- `cld devcontainer`: starts standard devcontainer (same as now)
- `cld devcontainer --master`: start master devcontainer in this repo, or if it's already running, returns to it's entrypoint (/workspace/current).
- `cld devcontainer shutdown [--all]`: shutdown master devcontainer in this repo (or all devcontainer across all repos if --all was specified)

3: Claude sandbox mode shouldn't be used at all inside devcontainer/agent. They are already sandboxed environments. 

4: I should have an access to the `cld` commands inside a devcontainer the exact same way as on host and they should work the exact same way.

5: `cld chain` should run in background after first initialization and log output with behaviour same as `cld agent`. A subcommand should be added (something along the lines of `cld chain status`) that will list the current status of all running chains within this repo. The status should contain on which stage out of total they are running.

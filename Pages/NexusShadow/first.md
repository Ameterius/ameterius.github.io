## Interface Basics
Upon launching, you will see the following:
```
INFO: Manual plugin loading is disabled.
NexusShadow Platform [Version 1.5 | .NET 4.8.1]
(c) Ametero (aka CanaryGen). All rights reserved.
```
- The first line is informational and indicates that manual plugin loading is disabled.
  (Note: `INFO: Manual plugin loading is disabled.` appears if manual plugin loading is turned off.)

The subsequent lines are copyright and version information.

## Commands
Unfortunately, the terminal initially supports only a few commands. Here is the list:
Available commands:
  - `plugin load <plugin>*` - Loads a plugin.
  - `plugin run <plugin>*`  - Runs a loaded plugin.
  - `plugin list*`          - Lists all loaded plugins.
  - `help`                 - Displays this page.
  - `exit`                 - Exits the application.

  *- Only available when manual plugin loading is enabled.

## Configuration
There is only one configuration setting in `appsettings.json`, which is:
```json
{
    "ManualPluginLoad": true
}
```
This setting controls manual plugin loading.

> ⚠️ Disabling this feature is not recommended, as the automatic loading system is experimental and unstable.

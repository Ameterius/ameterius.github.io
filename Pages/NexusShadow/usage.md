# Usage

## Basic Commands

- **help**: Show the list of available commands.
- **exit**: Exit the application.
- **plugin load <plugin>**: Load a plugin.
- **plugin run <plugin>**: Run a loaded plugin.
- **plugin list**: List all loaded plugins.

## Command Examples

```plaintext
> help
Available commands:
  plugin load <plugin> - Load a plugin
  plugin run <plugin>  - Run a loaded plugin
  plugin list          - List all loaded plugins
  help                 - Show this help message
  exit                 - Exit the application

> plugin load MessageSenderPlugin
Plugin 'MessageSenderPlugin' loaded.

> plugin run MessageSenderPlugin
Running plugin 'MessageSenderPlugin'.

> plugin list
MessageSenderPlugin: Sends a message to a specified IP address and port using UDP or TCP.

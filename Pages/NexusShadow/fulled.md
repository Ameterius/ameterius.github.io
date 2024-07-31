### Detailed Guide to Creating Plugins for NexusShadow Platform

#### Introduction

Plugins are a key part of the NexusShadow Platform, allowing you to extend its functionality and add new commands. This guide will help you understand how to create your own plugins.

#### Requirements

- Basic knowledge of C#.
- Visual Studio or another IDE for C# development.
- .NET Framework 4.8.1 or later.

#### Step 1: Creating the Project

1. **Open Visual Studio** and select "Create a new project".
2. Choose the "Class Library (.NET Framework)" template and click "Next".
3. **Name the project** (e.g., `MyCustomPlugin`) and specify the location to save the project.
4. Ensure that the .NET Framework 4.8.1 or later version is selected, and click "Create".

#### Step 2: Adding References

1. In the Solution Explorer, right-click on the project and select "Add Reference".
2. In the "Reference Manager" dialog, add references to the necessary assemblies:
   - `NexusShadow.Core.dll`
   - `NexusShadow.Plugins.dll`

#### Step 3: Implementing the IPlugin Interface

1. Open the `Class1.cs` file and rename it to `MyPlugin.cs`.
2. Implement the `IPlugin` interface in the `MyPlugin` class.

```csharp
using System;
using NexusShadow.Core;
using NexusShadow.Plugins;

namespace MyCustomPlugin
{
    public class MyPlugin : IPlugin
    {
        // IPlugin interface properties
        public string Name => "My Custom Plugin";
        public string Description => "A simple plugin that demonstrates the basics.";
        public string Author => "Your Name";

        // The Execute method performs the main logic of the plugin
        public void Execute()
        {
            Console.WriteLine("Hello from My Custom Plugin!");
        }

        // The RegisterCommands method registers the plugin's commands
        public void RegisterCommands(CommandRegistry registry)
        {
            registry.RegisterCommand("myplugin", args =>
            {
                Execute();
            });
        }
    }
}
```

**Code Explanations:**

- **IPlugin Interface Properties**:
  - `Name`: The name of the plugin.
  - `Description`: The description of the plugin.
  - `Author`: The author of the plugin.

- **Execute Method**:
  - This method performs the main logic of the plugin. In this example, it simply prints a message to the console.

- **RegisterCommands Method**:
  - This method registers the plugin's commands. In this example, it registers the `myplugin` command, which calls the `Execute` method.

#### Step 4: Compiling the Plugin

1. In the "Build" menu, select "Build Solution" (Ctrl + Shift + B).
2. Find the compiled DLL in the `bin/Debug` or `bin/Release` folder of your project.

#### Step 5: Deploying the Plugin

1. Copy the compiled DLL to the `Plugins` folder of your main project.
2. Ensure that the `Plugins` folder is located in the root directory of your main project.

#### Step 6: Loading and Using the Plugin

1. Run the main project and use the console interface to load and execute your plugin.
2. **Loading the plugin**:
   ```sh
   > plugin load MyCustomPlugin
   ```
3. **Executing the plugin**:
   ```sh
   > myplugin
   ```

#### Example of an Advanced Plugin

Here is an example of a more complex plugin that takes arguments and performs specific actions:

```csharp
using System;
using NexusShadow.Core;
using NexusShadow.Plugins;

namespace AdvancedPlugin
{
    public class AdvancedPlugin : IPlugin
    {
        public string Name => "Advanced Plugin";
        public string Description => "A more advanced plugin that takes arguments.";
        public string Author => "Your Name";

        public void Execute(string[] args)
        {
            if (args.Length == 0)
            {
                Console.WriteLine("Please provide an action.");
                return;
            }

            string action = args[0];
            switch (action.ToLower())
            {
                case "greet":
                    if (args.Length > 1)
                    {
                        Console.WriteLine($"Hello, {args[1]}!");
                    }
                    else
                    {
                        Console.WriteLine("Hello, World!");
                    }
                    break;
                case "farewell":
                    if (args.Length > 1)
                    {
                        Console.WriteLine($"Goodbye, {args[1]}!");
                    }
                    else
                    {
                        Console.WriteLine("Goodbye, World!");
                    }
                    break;
                default:
                    Console.WriteLine($"Unknown action: {action}");
                    break;
            }
        }

        public void RegisterCommands(CommandRegistry registry)
        {
            registry.RegisterCommand("advanced", args =>
            {
                Execute(args);
            });
        }
    }
}
```

**Code Explanations:**

- **Execute Method**:
  - This method takes a string array `args` that contains the command arguments.
  - If no arguments are provided, it prints a message asking for an action.
  - Depending on the value of the first argument (`action`), it performs the corresponding action (greeting or farewell).

- **RegisterCommands Method**:
  - Registers the `advanced` command, which calls the `Execute` method with the provided arguments.

#### Using Utilities

You can use utilities provided by the NexusShadow Platform to perform various tasks in your plugin. For example, you can use methods for logging, file operations, and configuration.

```csharp
using System;
using NexusShadow.Core;
using NexusShadow.Plugins;
using static NexusShadow.Core.Logger;
using static NexusShadow.Core.FileUtility;
using static NexusShadow.Core.ConfigurationManager;

namespace MyCustomPlugin
{
    public class MyPlugin : IPlugin
    {
        public string Name => "My Custom Plugin";
        public string Description => "A simple plugin that demonstrates the basics.";
        public string Author => "Your Name";

        public void Execute()
        {
            LogInfo("Executing My Custom Plugin.");

            string configValue = GetValue("SomeKey", "DefaultValue");
            LogInfo($"Configuration value for 'SomeKey': {configValue}");

            string filePath = "example.txt";
            string content = "Hello, World!";
            WriteFile(filePath, content);
            string fileContent = ReadFile(filePath);
            LogInfo($"File content: {fileContent}");
        }

        public void RegisterCommands(CommandRegistry registry)
        {
            registry.RegisterCommand("myplugin", args =>
            {
                Execute();
            });
        }
    }
}
```

**Code Explanations:**

- **Using Utilities**:
  - `LogInfo`: Logs an informational message.
  - `GetValue`: Retrieves a configuration value by key.
  - `WriteFile`: Writes content to a file.
  - `ReadFile`: Reads the content of a file.

#### Conclusion

Now you know how to create plugins for the NexusShadow Platform. Feel free to experiment and add new features to your plugins. If you have any questions or issues, don't hesitate to ask for help.

### Compilation and Running

1. **Build the project** (Ctrl + Shift + B).
2. **Run the project** (F5 or Ctrl + F5).

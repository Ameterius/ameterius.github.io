# Development

## IPlugin Interface

All plugins must implement the `IPlugin` interface, which defines the basic methods and properties:

```csharp
namespace NexusShadow.Plugins
{
    public interface IPlugin
    {
        string Name { get; }
        string Description { get; }
        string Author { get; }
        void Execute();
        void RegisterCommands(CommandRegistry registry);
    }
}
```

## Example Plugin

Example of a plugin that sends a message to a specified IP address and port:

```csharp
using System;
using System.Net;
using System.Net.Sockets;
using NexusShadow.Core;
using NexusShadow.Plugins;

namespace MessageSender
{
    public class MessageSenderPlugin : IPlugin
    {
        public string Name => "Message Sender";
        public string Description => "Sends a message to a specified IP address and port using UDP or TCP.";
        public string Author => "NexusShadow Team";

        private string ipAddress;
        private int port;
        private string mode;
        private string message;

        public void Execute()
        {
            Console.WriteLine($"Sending message '{message}' to {ipAddress}:{port} using {mode}...");

            if (mode.ToUpper() == "UDP")
            {
                SendUdpMessage();
            }
            else if (mode.ToUpper() == "TCP")
            {
                SendTcpMessage();
            }
            else
            {
                Console.WriteLine($"Unknown mode: {mode}");
            }
        }

        private void SendUdpMessage()
        {
            using (UdpClient udpClient = new UdpClient())
            {
                try
                {
                    udpClient.Connect(ipAddress, port);
                    byte[] sendBytes = System.Text.Encoding.ASCII.GetBytes(message);
                    udpClient.Send(sendBytes, sendBytes.Length);
                    Console.WriteLine("Message sent successfully using UDP.");
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"Failed to send message using UDP: {ex.Message}");
                }
            }
        }

        private void SendTcpMessage()
        {
            using (TcpClient tcpClient = new TcpClient())
            {
                try
                {
                    tcpClient.Connect(ipAddress, port);
                    using (NetworkStream stream = tcpClient.GetStream())
                    {
                        byte[] sendBytes = System.Text.Encoding.ASCII.GetBytes(message);
                        stream.Write(sendBytes, 0, sendBytes.Length);
                        Console.WriteLine("Message sent successfully using TCP.");
                    }
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"Failed to send message using TCP: {ex.Message}");
                }
            }
        }

        public void RegisterCommands(CommandRegistry registry)
        {
            registry.RegisterCommand("send", args =>
            {
                if (args.Length < 4)
                {
                    Console.WriteLine("Usage: send <ip> <port> <mode> <message>");
                    return;
                }

                ipAddress = args[0];
                port = int.Parse(args[1]);
                mode = args[2];
                message = args[3];

                Execute();
            });
        }
    }
}
```

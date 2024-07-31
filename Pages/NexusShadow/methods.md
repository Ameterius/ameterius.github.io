### Network Utility
`using static NexusShadow.Core.Subcore.NetworkUtility;`
--------------------------------------------------------------
`GetHttpContentAsync(string url)`
Asynchronously retrieves HTTP content from the specified URL.

Example:
```csharp
string content = await GetHttpContentAsync("https://example.com");
```

`PostHttpContentAsync(string url, string jsonContent)`
Asynchronously sends an HTTP POST request with JSON content.

Example:
```csharp
string response = await PostHttpContentAsync("https://example.com/api", "{\"key\":\"value\"}");
```

`PutHttpContentAsync(string url, string jsonContent)`
Asynchronously sends an HTTP PUT request with JSON content.

Example:
```csharp
string response = await PutHttpContentAsync("https://example.com/api", "{\"key\":\"value\"}");
```

`DeleteHttpContentAsync(string url)`
Asynchronously sends an HTTP DELETE request.

Example:
```csharp
bool success = await DeleteHttpContentAsync("https://example.com/api/resource");
```

`DownloadFileAsync(string url, string filePath)`
Asynchronously downloads a file from the URL and saves it to the specified path.

Example:
```csharp
bool success = await DownloadFileAsync("https://example.com/file.zip", "C:\\path\\to\\file.zip");
```

`SendTcpMessageAsync(string ipAddress, int port, string message)`
Asynchronously sends a TCP message to the specified IP address and port.

Example:
```csharp
string response = await SendTcpMessageAsync("127.0.0.1", 1234, "Hello, Server!");
```

`SendUdpMessageAsync(string ipAddress, int port, string message)`
Asynchronously sends a UDP message to the specified IP address and port.

Example:
```csharp
string response = await SendUdpMessageAsync("127.0.0.1", 1234, "Hello, Server!");
```

### Logging Utility
`using static NexusShadow.Core.Subcore.Logging;`
--------------------------------------------------------------
`LogInfo(string message)`
Logs an informational message.

Example:
```csharp
LogInfo("Application started.");
```

`LogError(string message)`
Logs an error message.

Example:
```csharp
LogError("An error occurred.");
```

### FileSystem Utility
`using static NexusShadow.Core.Subcore.FileSystem;`
--------------------------------------------------------------
`CreateDirectory(string path)`
Creates a new directory at the specified path.

Example:
```csharp
bool success = CreateDirectory("C:\\NewFolder");
```

`DeleteDirectory(string path, bool recursive = false)`
Deletes the directory at the specified path. If `recursive` is set to `true`, it deletes all contents recursively.

Example:
```csharp
bool success = DeleteDirectory("C:\\OldFolder", true);
```

`CopyFile(string sourcePath, string destinationPath)`
Copies a file from the source path to the destination path.

Example:
```csharp
bool success = CopyFile("C:\\Source\\file.txt", "C:\\Destination\\file.txt");
```

`DeleteFile(string path)`
Deletes the file at the specified path.

Example:
```csharp
bool success = DeleteFile("C:\\file.txt");
```

`ReadFileContent(string path)`
Reads the content of the file at the specified path.

Example:
```csharp
string content = ReadFileContent("C:\\file.txt");
```

`WriteFileContent(string path, string content)`
Writes content to the file at the specified path.

Example:
```csharp
bool success = WriteFileContent("C:\\file.txt", "New content");
```

`MoveFile(string sourcePath, string destinationPath)`
Moves a file from the source path to the destination path.

Example:
```csharp
bool success = MoveFile("C:\\Source\\file.txt", "C:\\Destination\\file.txt");
```

`MoveDirectory(string sourcePath, string destinationPath)`
Moves a directory from the source path to the destination path.

Example:
```csharp
bool success = MoveDirectory("C:\\Source\\Folder", "C:\\Destination\\Folder");
```

`FileExists(string path)`
Checks if a file exists at the specified path.

Example:
```csharp
bool exists = FileExists("C:\\file.txt");
```

`DirectoryExists(string path)`
Checks if a directory exists at the specified path.

Example:
```csharp
bool exists = DirectoryExists("C:\\Folder");
```

`GetFiles(string path)`
Returns a list of files in the specified directory.

Example:
```csharp
string[] files = GetFiles("C:\\Folder");
```

`GetDirectories(string path)`
Returns a list of subdirectories in the specified directory.

Example:
```csharp
string[] directories = GetDirectories("C:\\Folder");
```

`ReadFileLines(string path)`
Reads the lines of the file at the specified path.

Example:
```csharp
IEnumerable<string> lines = ReadFileLines("C:\\file.txt");
```

`WriteFileLines(string path, IEnumerable<string> lines)`
Writes lines to the file at the specified path.

Example:
```csharp
bool success = WriteFileLines("C:\\file.txt", new List<string> { "Line1", "Line2" });
```

`GetFileSize(string path)`
Returns the size of the file in bytes.

Example:
```csharp
long size = GetFileSize("C:\\file.txt");
```

`GetDirectorySize(string path)`
Returns the size of the directory in bytes.

Example:
```csharp
long size = GetDirectorySize("C:\\Folder");
```

`ClearDirectory(string path)`
Deletes all files and subdirectories in the specified directory.

Example:
```csharp
bool success = ClearDirectory("C:\\Folder");
```

`CopyDirectory(string sourcePath, string destinationPath)`
Copies a directory from the source path to the destination path.

Example:
```csharp
bool success = CopyDirectory("C:\\Source\\Folder", "C:\\Destination\\Folder");
```

`RenameFile(string path, string newName)`
Renames the file at the specified path.

Example:
```csharp
bool success = RenameFile("C:\\file.txt", "newfile.txt");
```

`RenameDirectory(string path, string newName)`
Renames the directory at the specified path.

Example:
```csharp
bool success = RenameDirectory("C:\\Folder", "NewFolder");
```

### EncryptionHelper
`using static NexusShadow.Core.Subcore.EncryptionHelper;`
--------------------------------------------------------------
`Bind(int id, string key, string iv)`
Binds an encryption configuration with the specified ID, key, and initialization vector.

Example:
```csharp
bool success = Bind(1, "mykey123", "myiv123");
```

`Unbind(int id)`
Removes the encryption configuration with the specified ID.

Example:
```csharp
bool success = Unbind(1);
```

`Encrypt(int id, string plainText)`
Encrypts the text using the encryption configuration with the specified ID.

Example:
```csharp
string encryptedText = Encrypt(1, "secret message");
```

`Decrypt(int id, string cipherText)`
Decrypts the text using the encryption configuration with the specified ID.

Example:
```csharp
string decryptedText = Decrypt(1, encryptedText);
```

### Email Utility
`using static NexusShadow.Core.Subcore.EmailUttil;`
--------------------------------------------------------------
`SMTPBind(string smtpServer, int smtpPort, string smtpUsername, string smtpPassword, int smtpID)`
Binds an SMTP configuration with the specified ID, server, port, username, and password.

Example:
```csharp
bool success = SMTPBind("smtp.example.com", 587, "user@example.com", "password", 1);
```

`SMTPUnbind(int smtpID)`
Removes the SMTP configuration with the specified ID.

Example:
```csharp
bool success = SMTPUnbind(1);
```

`SendEmail(int smtpID, string to, string subject, string body)`
Sends an email using the SMTP configuration with the specified ID.

Example:
```csharp
bool success = SendEmail(1, "recipient@example.com", "Subject", "Body");
```

### Date Utility
`using static NexusShadow.Core.Subcore.DateUtillity;`
--------------------------------------------------------------
`GetCurrentDateTimeString()`
Returns the current date and time in the string format "yyyy-MM-dd HH:mm:ss".

Example:
```csharp
string currentDateTime = GetCurrentDateTimeString();
```

`IsWeekend(DateTime date)`
Checks if the specified date is a weekend (Saturday or Sunday).

Example:
```csharp
bool isWeekend = IsWeekend(new DateTime(2023, 10, 15)); // true if October 15, 2023 is Sunday
```

`GetNextWorkday(DateTime date)`
Returns the next workday after the specified date.

Example:
```csharp
DateTime nextWorkday = GetNextWorkday(new DateTime(2023, 10, 14)); // October 16, 2023 if October 14, 2023 is Saturday
```

### QueueHelper
`using static NexusShadow.Core.Subcore.QueueHelper;`
--------------------------------------------------------------
`EnqueueTask(Action task)`
Adds a task to the queue for asynchronous processing.

Example:
```csharp
EnqueueTask(() => Console.WriteLine("Task executed"));
```

`ProcessQueueAsync()`
Asynchronously processes all tasks in the queue.

Example:
```csharp
await ProcessQueueAsync();
```

### StringHelper
`using static NexusShadow.Core.Subcore.StringHelper;`
--------------------------------------------------------------
`Reverse(string input)`
Returns the string reversed.

Example:
```csharp
string reversed = Reverse("hello"); // "olleh"
```

`Replace(string input, string oldValue, string newValue)`
Replaces all occurrences of `oldValue` with `newValue` in the string `input`.

Example:
```csharp
string replaced = Replace("hello world", "world", "everyone"); // "hello everyone"
```

`RemoveWhitespace(string input)`
Removes all whitespace from the string.

Example:
```csharp
string noWhitespace = RemoveWhitespace("hello world"); // "helloworld"
```

### ThreadingHelper
`using static NexusShadow.Core.Subcore.ThreadingHelper;`
--------------------------------------------------------------
`RunInBackground(Action action)`
Runs the specified action in a background thread.

Example:
```csharp
RunInBackground(() => Console.WriteLine("Background task"));
```

`RunInBackgroundAsync(Func<Task> asyncAction)`
Runs the specified asynchronous action in a background thread.

Example:
```csharp
await RunInBackgroundAsync(async () => await Task.Delay(1000));
```

`Sleep(int milliseconds)`
Pauses the current thread for the specified number of milliseconds.

Example:
```csharp
Sleep(1000); // Wait for 1 second
```

### XMLConfig
`using static NexusShadow.Core.Subcore.XMLConfig;`
--------------------------------------------------------------
`Initialize(string filePath)`
Initializes the configuration from the XML file at the specified path.

Example:
```csharp
Initialize("C:\\config.xml");
```

`Save()`
Saves the current configuration to the XML file.

Example:
```csharp
bool success = Save();
```

`GetValue<T>(string key, T defaultValue = default)`
Returns the value from the configuration for the specified key. If the key is not found, it returns `defaultValue`.

Example:
```csharp
string value = GetValue<string>("SettingKey", "default");
```

`SetValue(string key, object value)`
Sets the value in the configuration for the specified key.

Example:
```csharp
SetValue("SettingKey", "newValue");
```

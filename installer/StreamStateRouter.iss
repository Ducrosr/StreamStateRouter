#define MyAppName "Stream State Router"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#define MyAppPublisher "Ducrosr"
#define MyAppExeName "StreamStateRouter.exe"

[Setup]
AppId={{AE0F8714-BF63-468B-A58D-08F6D36B582A}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppMutex=Local\StreamStateRouter-v1
DefaultDirName={autopf}\Stream State Router
DefaultGroupName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\release
OutputBaseFilename=Stream-State-Router-v{#MyAppVersion}-setup
PrivilegesRequired=lowest

[Files]
Source: "..\dist\StreamStateRouter.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Créer un raccourci sur le Bureau"; GroupDescription: "Raccourcis :"; Flags: unchecked

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Lancer {#MyAppName}"; Flags: nowait postinstall skipifsilent

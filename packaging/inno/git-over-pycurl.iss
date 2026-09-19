#ifndef AppName
  #define AppName "git-over-pycurl"
#endif
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef AppPublisher
  #define AppPublisher "ayazmur"
#endif
#ifndef AppUrl
  #define AppUrl "https://github.com/ayazmur/girovercurl"
#endif
#ifndef AppExeName
  #define AppExeName "git-over-pycurl.exe"
#endif
#ifndef SourceExe
  #define SourceExe "..\..\dist\git-over-pycurl.exe"
#endif

[Setup]
AppId={{9E2B7A64-1F3C-4B7E-9A21-6C5D0E8F4A12}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}
AppUpdatesURL={#AppUrl}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=git-over-pycurl-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ChangesEnvironment=yes
UninstallDisplayName={#AppName}

[Files]
Source: "{#SourceExe}"; DestDir: "{app}"; Flags: ignoreversion

[Tasks]
Name: "addtopath"; Description: "Add {#AppName} to PATH"; GroupDescription: "Additional options:"; Flags: checkedonce

[Registry]
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Flags: preservestringtype; Tasks: addtopath; Check: NeedsAddPath

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Parameters: "status"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\{#AppExeName}"; Parameters: "status"; Description: "Show {#AppName} status"; Flags: postinstall nowait skipifsilent

[Code]
function NeedsAddPath(): Boolean;
var
  CurrentPath: String;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', CurrentPath) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Uppercase(ExpandConstant('{app}')) + ';', ';' + Uppercase(CurrentPath) + ';') = 0;
end;

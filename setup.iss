; Inno Setup 6/7 Script for Getsu AI Real-Time Noise Cancellation
; ==============================================================

#define MyAppName "Getsu"
#define MyAppVersion "1.1.0"
#define MyAppPublisher "skttr87"
#define MyAppURL "https://github.com/skttr87/getsu"
#define MyAppExeName "getsu.exe"

[Setup]
AppId={{8B4F923D-567A-4831-9F3B-2A58C7E1B455}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\Getsu
AppendDefaultDirName=yes
DisableDirPage=no
DisableProgramGroupPage=yes
PrivilegesRequired=admin
OutputDir=dist\installer
OutputBaseFilename=Getsu-v1.1.0-Setup
SetupIconFile=getsu.ico
MinVersion=10.0.10240
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\getsu.exe
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
VersionInfoVersion=1.1.0.0
VersionInfoCompany=Getsu AI
VersionInfoDescription=Getsu - AI Real-Time Noise Cancellation Setup
VersionInfoCopyright=Copyright (c) 2026 skttr87 (MIT License)
VersionInfoProductName=Getsu AI Noise Cancellation
VersionInfoProductVersion=1.1.0
VersionInfoOriginalFileName=Getsu-v1.1.0-Setup.exe

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
FinishedLabel=Setup has finished installing [name] on your computer.%n%nNotice: Keep your normal Speakers or Headphones selected for your Windows sound (taskbar volume icon). Set 'CABLE Output' only as your Microphone inside Discord, Steam, or Zoom.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Compiled application files and bundled internal dependencies
Source: "dist\getsu\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Bundled virtual audio cable driver
Source: "drivers\vbcable\*"; DestDir: "{app}\drivers\vbcable"; Flags: ignoreversion recursesubdirs createallsubdirs
; Helper tool to detect endpoints during installer initialization
Source: "drivers\vbcable\AudioRestore.exe"; Flags: dontcopy
; Project assets & license
Source: "LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "getsu.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\getsu.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\getsu.ico"; Tasks: desktopicon

[UninstallDelete]
Type: files; Name: "{app}\config.json"
Type: files; Name: "{app}\*.log"
Type: dirifempty; Name: "{app}\drivers\vbcable"
Type: dirifempty; Name: "{app}\drivers"
Type: dirifempty; Name: "{app}\_internal"
Type: dirifempty; Name: "{app}"

[Code]
// Detect if VB-Audio Virtual Cable endpoint is actually active on this computer
function IsVBCableInstalled(): Boolean;
var
  ResultCode: Integer;
  AudioRestoreExe: String;
begin
  Result := False;
  try
    ExtractTemporaryFile('AudioRestore.exe');
    AudioRestoreExe := ExpandConstant('{tmp}\AudioRestore.exe');
    if FileExists(AudioRestoreExe) then
    begin
      Exec(AudioRestoreExe, '--check-vbcable', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      Result := (ResultCode = 0);
    end;
  except
    Result := False;
  end;
end;

var
  RemoveVBCableRequested: Boolean;

// Enforce that installation folder always has a dedicated \Getsu subfolder
function NextButtonClick(CurPageID: Integer): Boolean;
var
  SelectedPath: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    SelectedPath := WizardDirValue;
    // If the chosen directory name is not already 'Getsu', append \Getsu
    if Uppercase(ExtractFileName(SelectedPath)) <> 'GETSU' then
    begin
      WizardForm.DirEdit.Text := AddBackslash(SelectedPath) + 'Getsu';
    end;
  end;
end;

// Ask user during uninstall if they also want to remove VB-Cable
function InitializeUninstall(): Boolean;
var
  ResultCode: Integer;
  AudioRestoreExe: String;
  IsInstalled: Boolean;
begin
  Result := True;
  RemoveVBCableRequested := False;
  IsInstalled := False;

  AudioRestoreExe := ExpandConstant('{app}\drivers\vbcable\AudioRestore.exe');
  if FileExists(AudioRestoreExe) then
  begin
    Exec(AudioRestoreExe, '--check-vbcable', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    IsInstalled := (ResultCode = 0);
  end;

  if IsInstalled then
  begin
    if MsgBox('Do you also want to remove the VB-Audio Virtual Cable driver from your system?' + #13#10 + #13#10 +
              '(Note: If other applications like OBS or Discord use this cable, select "No")',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    begin
      RemoveVBCableRequested := True;
    end;
  end;
end;

// Execute driver uninstaller before Getsu files are removed, then clean up leftover files
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
  DriverSetupExe: String;
  AppDir: String;
begin
  if (CurUninstallStep = usUninstall) and RemoveVBCableRequested then
  begin
    DriverSetupExe := ExpandConstant('{app}\drivers\vbcable\VBCABLE_Setup_x64.exe');
    if FileExists(DriverSetupExe) then
    begin
      Exec(DriverSetupExe, '-u -h', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    end;
  end;

  if CurUninstallStep = usPostUninstall then
  begin
    AppDir := ExpandConstant('{app}');
    // Only delete Getsu's runtime config file
    if FileExists(AddBackslash(AppDir) + 'config.json') then
      DeleteFile(AddBackslash(AppDir) + 'config.json');

    // Safely remove folders ONLY if completely empty (Win32 RemoveDirectory)
    // This strictly protects user files: if the folder contains personal documents or is a user profile,
    // Windows refuses to delete it!
    RemoveDir(AddBackslash(AppDir) + 'drivers\vbcable');
    RemoveDir(AddBackslash(AppDir) + 'drivers');
    RemoveDir(AddBackslash(AppDir) + '_internal');
    RemoveDir(AppDir);
  end;
end;

// Automatically install VB-Cable if needed, preserving physical speakers as default playback device
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  DriverSetupExe: String;
  AudioRestoreExe: String;
  BackupFile: String;
  NeedsInstall: Boolean;
begin
  if CurStep = ssPostInstall then
  begin
    AudioRestoreExe := ExpandConstant('{app}\drivers\vbcable\AudioRestore.exe');
    DriverSetupExe := ExpandConstant('{app}\drivers\vbcable\VBCABLE_Setup_x64.exe');
    BackupFile := ExpandConstant('{tmp}\default_audio_backup.txt');

    NeedsInstall := True;
    if FileExists(AudioRestoreExe) then
    begin
      Exec(AudioRestoreExe, '--check-vbcable', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      NeedsInstall := (ResultCode <> 0);
    end;

    if NeedsInstall then
    begin
      // 1. Backup current default playback device (Speakers / Headphones)
      if FileExists(AudioRestoreExe) then
      begin
        Exec(AudioRestoreExe, '--backup "' + BackupFile + '"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      end;

      // 2. Silently install VB-Cable driver
      if FileExists(DriverSetupExe) then
      begin
        Exec(DriverSetupExe, '-i -h', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
        Sleep(1500);
      end;

      // 3. Immediately restore original physical speakers/headphones as default Windows output
      if FileExists(AudioRestoreExe) then
      begin
        Exec(AudioRestoreExe, '--restore "' + BackupFile + '"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
        Exec(AudioRestoreExe, '--ensure-physical', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      end;
    end
    else
    begin
      // If already installed, just ensure physical speakers remain default
      if FileExists(AudioRestoreExe) then
      begin
        Exec(AudioRestoreExe, '--ensure-physical', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      end;
    end;
  end;
end;

[Run]
; Post-install launch option
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

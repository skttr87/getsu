; Inno Setup 6/7 Script for Getsu AI Real-Time Noise Cancellation
; ==============================================================

#define MyAppName "Getsu"
#define MyAppVersion "1.2.0"
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
OutputBaseFilename=Getsu-v1.2.0-Setup
SetupIconFile=getsu.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\getsu.exe
CloseApplications=force
RestartApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
VersionInfoVersion=1.2.0.0
VersionInfoCompany=skttr87
VersionInfoDescription=Getsu - AI Real-Time Noise Cancellation Setup
VersionInfoCopyright=Copyright (c) 2026 Ihsan (@skttr87)
VersionInfoProductName=Getsu AI Noise Cancellation
VersionInfoProductVersion=1.2.0
VersionInfoOriginalFileName=Getsu-v1.2.0-Setup.exe

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
UninstallAppTitle=Getsu Uninstaller
UninstallAppFullTitle=Getsu Uninstaller
FinishedLabel=Setup has finished installing [name] on your computer.%n%nNotice: Keep your normal Speakers or Headphones selected for your Windows sound (taskbar volume icon). Getsu will automatically route your clean microphone to your games and voice apps.

[Tasks]
Name: "installvbcable"; Description: "Install VB-Audio Virtual Cable (Recommended: routes clean audio to all voice apps and games)"; GroupDescription: "Virtual Audio Components:"; Flags: checkedonce
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Compiled application files and bundled internal dependencies (preserve existing user config.json on upgrade)
Source: "dist\getsu\*"; DestDir: "{app}"; Excludes: "config.json"; Flags: ignoreversion recursesubdirs createallsubdirs
; Default config file: only installed if config.json does not already exist in destination
Source: "config.json"; DestDir: "{app}"; Flags: onlyifdoesntexist
; Bundled virtual audio cable driver
Source: "drivers\vbcable\*"; DestDir: "{app}\drivers\vbcable"; Flags: ignoreversion recursesubdirs createallsubdirs
; Project assets & license
Source: "LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "getsu.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\getsu.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\getsu.ico"; Tasks: desktopicon

[InstallDelete]
Type: files; Name: "{autoprograms}\Getsu Uninstaller.lnk"

[UninstallDelete]
Type: files; Name: "{app}\config.json"
Type: files; Name: "{app}\*.log"
Type: dirifempty; Name: "{app}\drivers\vbcable"
Type: dirifempty; Name: "{app}\drivers"
Type: dirifempty; Name: "{app}\_internal"
Type: dirifempty; Name: "{app}"

[Code]
const
  HWND_TOPMOST = -1;
  HWND_NOTOPMOST = -2;
  SWP_NOSIZE = $0001;
  SWP_NOMOVE = $0002;
  SWP_SHOWWINDOW = $0040;

var
  RemoveVBCableRequested: Boolean;
  FirstPageFocused: Boolean;

function SetWindowPos(hWnd: HWND; hWndInsertAfter: Integer; X, Y, cx, cy: Integer; uFlags: Cardinal): Boolean;
  external 'SetWindowPos@user32.dll stdcall';
function SetForegroundWindow(hWnd: HWND): Boolean;
  external 'SetForegroundWindow@user32.dll stdcall';
function BringWindowToTop(hWnd: HWND): Boolean;
  external 'BringWindowToTop@user32.dll stdcall';

// Bring installer window to front and grant foreground focus upon launch
procedure ForceForegroundWindow(hWnd: HWND);
begin
  if hWnd = 0 then Exit;

  // 1. Z-Order: Pop window physically in front of all background apps
  SetWindowPos(hWnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE or SWP_NOSIZE or SWP_SHOWWINDOW);

  // 2. Active Focus: Grab foreground activation while topmost
  BringWindowToTop(hWnd);
  SetForegroundWindow(hWnd);

  // 3. Clear topmost flag so it behaves like a standard window if user switches away
  SetWindowPos(hWnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE or SWP_NOSIZE or SWP_SHOWWINDOW);
end;

procedure InitializeWizard();
begin
  WizardForm.BringToFront();
  ForceForegroundWindow(WizardForm.Handle);
end;

// Ensure first rendered wizard page maintains active window focus after UAC elevation
procedure CurPageChanged(CurPageID: Integer);
begin
  if not FirstPageFocused then
  begin
    FirstPageFocused := True;
    ForceForegroundWindow(WizardForm.Handle);
  end;
end;

// Compares two semantic version strings (e.g. "1.2.0" and "1.1.0")
// Returns: 1 if Ver1 > Ver2, -1 if Ver1 < Ver2, 0 if equal
function CompareVersion(Ver1, Ver2: String): Integer;
var
  P1, P2: Integer;
  Num1, Num2: Integer;
  Part1, Part2: String;
begin
  Result := 0;
  while (Length(Ver1) > 0) or (Length(Ver2) > 0) do
  begin
    P1 := Pos('.', Ver1);
    if P1 > 0 then
    begin
      Part1 := Copy(Ver1, 1, P1 - 1);
      Delete(Ver1, 1, P1);
    end
    else
    begin
      Part1 := Ver1;
      Ver1 := '';
    end;

    P2 := Pos('.', Ver2);
    if P2 > 0 then
    begin
      Part2 := Copy(Ver2, 1, P2 - 1);
      Delete(Ver2, 1, P2);
    end
    else
    begin
      Part2 := Ver2;
      Ver2 := '';
    end;

    Num1 := StrToIntDef(Part1, 0);
    Num2 := StrToIntDef(Part2, 0);

    if Num1 > Num2 then
    begin
      Result := 1;
      Exit;
    end
    else if Num1 < Num2 then
    begin
      Result := -1;
      Exit;
    end;
  end;
end;

// Queries Windows registry for currently installed Getsu version
function GetInstalledVersion(): String;
var
  Key: String;
  Ver: String;
begin
  Result := '';
  Key := 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{8B4F923D-567A-4831-9F3B-2A58C7E1B455}_is1';
  if RegQueryStringValue(HKLM64, Key, 'DisplayVersion', Ver) then
    Result := Ver
  else if RegQueryStringValue(HKLM, Key, 'DisplayVersion', Ver) then
    Result := Ver
  else if RegQueryStringValue(HKCU64, Key, 'DisplayVersion', Ver) then
    Result := Ver
  else if RegQueryStringValue(HKCU, Key, 'DisplayVersion', Ver) then
    Result := Ver;
end;

// Downgrade Protection: Prevent older installers from running over newer versions
function InitializeSetup(): Boolean;
var
  InstalledVer: String;
begin
  Result := True;
  InstalledVer := GetInstalledVersion();
  if InstalledVer <> '' then
  begin
    if CompareVersion(InstalledVer, '{#MyAppVersion}') > 0 then
    begin
      if not WizardSilent then
      begin
        MsgBox('A newer version of Getsu (' + InstalledVer + ') is already installed on your computer.' + #13#10 + #13#10 +
               'Installing an older version (' + '{#MyAppVersion}' + ') is not supported.' + #13#10 + #13#10 +
               'If you wish to downgrade, please uninstall the newer version first via Windows Settings -> Installed Apps.',
               mbError, MB_OK);
      end;
      Result := False;
    end;
  end;
end;

// Automatically and cleanly terminate running Getsu processes before files are overwritten
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  AppDir: String;
  AudioRestoreExe: String;
begin
  Result := '';
  AppDir := ExpandConstant('{app}');
  AudioRestoreExe := AddBackslash(AppDir) + 'drivers\vbcable\AudioRestore.exe';

  // 1. If AudioRestore exists from prior installation, ensure microphone & playback are restored to physical hardware
  if FileExists(AudioRestoreExe) then
  begin
    Exec(AudioRestoreExe, '--ensure-physical-capture', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec(AudioRestoreExe, '--ensure-physical', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;

  // 2. Forcibly close any running getsu.exe processes so binaries and DLLs in {app} are unlocked
  Exec('taskkill.exe', '/F /T /IM getsu.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

  // Give Windows 500ms to flush file handles
  Sleep(500);
end;

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
  AppDir: String;
  AudioRestoreExe: String;
  IsInstalled: Boolean;
begin
  Result := True;
  RemoveVBCableRequested := False;
  IsInstalled := False;

  AppDir := ExpandConstant('{app}');
  AudioRestoreExe := AddBackslash(AppDir) + 'drivers\vbcable\AudioRestore.exe';

  // 1. Immediately restore default microphone & playback device to physical hardware
  if FileExists(AudioRestoreExe) then
  begin
    Exec(AudioRestoreExe, '--ensure-physical-capture', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec(AudioRestoreExe, '--ensure-physical', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;

  // 2. Forcibly terminate running getsu.exe processes so binaries and DLLs can be completely removed
  Exec('taskkill.exe', '/F /T /IM getsu.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

  // Give Windows 500ms to flush file handles
  Sleep(500);

  // 3. Check if VB-Cable is installed
  if FileExists(AudioRestoreExe) then
  begin
    Exec(AudioRestoreExe, '--check-vbcable', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    IsInstalled := (ResultCode = 0);
  end;

  if IsInstalled then
  begin
    if MsgBox('Do you also want to remove the VB-Audio Virtual Cable driver from your system?' + #13#10 + #13#10 +
              '(Note: If other voice or streaming software uses this driver, select "No")',
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
  AudioRestoreExe: String;
  AppDir: String;
begin
  AppDir := ExpandConstant('{app}');

  if CurUninstallStep = usUninstall then
  begin
    AudioRestoreExe := AddBackslash(AppDir) + 'drivers\vbcable\AudioRestore.exe';

    // Ensure audio endpoints are physical and getsu is dead before file deletion
    if FileExists(AudioRestoreExe) then
    begin
      Exec(AudioRestoreExe, '--ensure-physical-capture', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      Exec(AudioRestoreExe, '--ensure-physical', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    end;
    Exec('taskkill.exe', '/F /T /IM getsu.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Sleep(300);

    if RemoveVBCableRequested then
    begin
      DriverSetupExe := AddBackslash(AppDir) + 'drivers\vbcable\VBCABLE_Setup_x64.exe';
      if FileExists(DriverSetupExe) then
      begin
        Exec(DriverSetupExe, '-u -h', AddBackslash(AppDir) + 'drivers\vbcable', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      end;
    end;
  end;

  if CurUninstallStep = usPostUninstall then
  begin
    // Clean up local config file if present
    if FileExists(AddBackslash(AppDir) + 'config.json') then
      DeleteFile(AddBackslash(AppDir) + 'config.json');

    // Clean up %APPDATA%\Getsu\config.json and folder
    if FileExists(ExpandConstant('{userappdata}\Getsu\config.json')) then
      DeleteFile(ExpandConstant('{userappdata}\Getsu\config.json'));
    RemoveDir(ExpandConstant('{userappdata}\Getsu'));

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

    // Only run driver setup in interactive mode if explicitly selected by the user.
    // In silent/unattended mode (e.g. winget /VERYSILENT), driver install is skipped to avoid modal dialog hangs.
    // Getsu will prompt cleanly in-app upon first launch if the driver is not yet present.
    if NeedsInstall and (not WizardSilent) and WizardIsTaskSelected('installvbcable') then
    begin
      // 1. Backup current default playback device (Speakers / Headphones)
      if FileExists(AudioRestoreExe) then
      begin
        Exec(AudioRestoreExe, '--backup "' + BackupFile + '"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      end;

      // 2. Install VB-Cable driver
      if FileExists(DriverSetupExe) then
      begin
        Exec(DriverSetupExe, '-i -h', ExpandConstant('{app}\drivers\vbcable'), SW_HIDE, ewWaitUntilTerminated, ResultCode);
        Sleep(2500);
      end;

      // 3. Immediately restore original physical speakers/headphones as default Windows output
      if FileExists(AudioRestoreExe) then
      begin
        Exec(AudioRestoreExe, '--restore "' + BackupFile + '"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
        Exec(AudioRestoreExe, '--ensure-physical', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
        Exec(AudioRestoreExe, '--ensure-physical-capture', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      end;
    end
    else
    begin
      // If already installed, just ensure physical speakers & microphone remain default
      if FileExists(AudioRestoreExe) then
      begin
        Exec(AudioRestoreExe, '--ensure-physical', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
        Exec(AudioRestoreExe, '--ensure-physical-capture', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      end;
    end;
  end;
end;

[Run]
; Post-install launch option (runs as standard user with app directory as CWD)
Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent runasoriginaluser

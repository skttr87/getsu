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
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\getsu.exe
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

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
; Project assets & license
Source: "LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "getsu.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\getsu.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\getsu.ico"; Tasks: desktopicon

[Code]
// Detect if VB-Audio Virtual Cable is already installed
function IsVBCableInstalled(): Boolean;
begin
  // Check the Windows kernel service entry created by the VB-Audio driver
  Result := RegKeyExists(HKLM, 'SYSTEM\CurrentControlSet\Services\VBAudioVACMME') or
            RegKeyExists(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\VB:VBCABLE') or
            RegKeyExists(HKLM, 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\VB:VBCABLE');
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
begin
  Result := True;
  RemoveVBCableRequested := False;
  if IsVBCableInstalled() then
  begin
    if MsgBox('Do you also want to remove the VB-Audio Virtual Cable driver from your system?' + #13#10 + #13#10 +
              '(Note: If other applications like OBS or Discord use this cable, select "No")',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    begin
      RemoveVBCableRequested := True;
    end;
  end;
end;

// Execute driver uninstaller before Getsu application files are removed
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
  DriverSetupExe: String;
begin
  if (CurUninstallStep = usUninstall) and RemoveVBCableRequested then
  begin
    DriverSetupExe := ExpandConstant('{app}\drivers\vbcable\VBCABLE_Setup_x64.exe');
    if FileExists(DriverSetupExe) then
    begin
      Exec(DriverSetupExe, '-u -h', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    end;
  end;
end;

[Run]
; Silently install VB-Cable driver if NOT already present on this computer
Filename: "{app}\drivers\vbcable\VBCABLE_Setup_x64.exe"; Parameters: "-i -h"; \
  StatusMsg: "Detecting and configuring audio drivers..."; \
  Check: not IsVBCableInstalled; Flags: runhidden waituntilterminated

; Post-install launch option
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

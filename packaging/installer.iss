; Inno Setup script for the Galil Controller Suite.
;
; Compiled by packaging/build_installer.py, which runs PyInstaller first and
; passes MyAppVersion in. Compiling this file on its own works too, as long as
; packaging\_dist\GalilControllerSuite already exists.
;
; Design notes:
;  * Per-user install by default (PrivilegesRequiredOverridesAllowed), so a peer
;    without administrator rights can still install. The default directory is
;    under Local AppData in that case and Program Files when elevated - Inno
;    resolves {autopf} accordingly.
;  * The app never writes to its install directory. Config and logs live in
;    %LOCALAPPDATA%\GalilControllerSuite (see utils/app_paths.py), which is why
;    a non-admin install into Program Files is still safe.
;  * That user data is deliberately NOT removed on uninstall. It holds the rig's
;    declared zeros, the saved positions and the Z retract state - losing those
;    to an uninstall/reinstall cycle would silently discard calibration. The
;    uninstaller offers to remove it instead.

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif

#define MyAppName "Galil Controller Suite"
#define MyAppExeName "GalilControllerSuite.exe"
#define MyAppPublisher "Technion - Light Injector"
#define MyAppId "{{8F3C6A21-5D74-4E9B-9C2E-1A7B45D0E6F2}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\GalilControllerSuite
DefaultGroupName={#MyAppName}
; Let the user pick the folder - the whole point of shipping an installer.
DisableDirPage=no
DisableProgramGroupPage=yes
AllowNoIcons=yes
OutputDir=..\packaging\_output
OutputBaseFilename=GalilControllerSuite-Setup-{#MyAppVersion}
SetupIconFile=galil_suite.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 64-bit only: the bundled Python, Qt and gclib DLLs are all x64.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
LicenseFile=
InfoBeforeFile=

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
; The entire PyInstaller one-folder build. The interpreter, PyQt5, numpy,
; pydantic, loguru, the gclib DLLs and the default config all live in here, so
; the target machine needs no Python and no pip.
Source: "_dist\GalilControllerSuite\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{group}\{#MyAppName} (simulator)"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--mock"; IconFilename: "{app}\{#MyAppExeName}"; Comment: "Run against a simulated controller - no hardware needed"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
// Offer to delete the per-user config on uninstall. Default is No: it carries
// zero_offsets.json, the rig's declared origins, which are measured rather than
// guessable - see CLAUDE.md on the micron origin.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\GalilControllerSuite');
    if DirExists(DataDir) then
    begin
      if MsgBox('Also delete your settings and calibration?' + #13#10#13#10 +
                DataDir + #13#10#13#10 +
                'This includes the declared zero offsets, saved positions and ' +
                'the Z retract state. Choose No to keep them for a reinstall.',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;

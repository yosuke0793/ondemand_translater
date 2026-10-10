#ifndef AppVersion
  #error AppVersion must be supplied by the build
#endif
#ifndef BundleDir
  #error BundleDir must be supplied by the build
#endif
#ifndef OutputDir
  #error OutputDir must be supplied by the build
#endif

[Setup]
AppId={{F0D51D16-E7B6-4488-B3B3-0DB6406D9C4C}
AppName=TranslateService
AppVersion={#AppVersion}
AppPublisher=Yosuke YAMANAKA
DefaultDirName={localappdata}\Programs\TranslateService
DefaultGroupName=TranslateService
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=TranslateService-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\TranslateService.exe
CloseApplications=yes
RestartApplications=no
LicenseFile={#BundleDir}\_internal\third-party-notices\TranslateService-LICENSE.txt

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\TranslateService"; Filename: "{app}\TranslateService.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\TranslateService"; Filename: "{app}\TranslateService.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Run]
Filename: "{app}\TranslateService.exe"; Description: "Launch TranslateService"; Flags: nowait postinstall skipifsilent

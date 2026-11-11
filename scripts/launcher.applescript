-- StudioLaser launcher: starts the desktop workspace from the project's .venv.

set appPath to POSIX path of (path to me)
-- The app is expected to live in the project root (see README). Resolve the
-- folder that contains the .app bundle.
set projectPath to do shell script ("dirname " & quoted form of appPath)
set desktopApp to projectPath & "/.venv/bin/studiolaser-desktop"
try
    do shell script ("test -x " & quoted form of desktopApp)
on error
    display dialog "StudioLaser.app must stay in the StudioLaser project folder (next to .venv). Could not find " & desktopApp & ". Run 'Install StudioLaser.command' again." buttons {"OK"} default button "OK" with icon stop
    return
end try

-- Detach so this launcher exits while the workspace keeps running.
do shell script "cd " & quoted form of projectPath & " && nohup " & quoted form of desktopApp & " >/dev/null 2>&1 &"

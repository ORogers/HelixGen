-- import_helix_preset.applescript
-- Automates HX Edit import flow. Supports automatic first-slot overwrite (default)
-- and a manual mode that allows the user to choose the target slot before import.

on run argv
	if (count of argv) = 0 then error "Preset path argument is required."
	set presetPath to (item 1 of argv) as text
	set scriptMode to "auto"
	if (count of argv) ≥ 2 then set scriptMode to (item 2 of argv) as text
	set manualMode to (scriptMode is equal to "manual")

	tell application "HX Edit" to activate
	delay 3

	if manualMode then
		display dialog "Click Continue, then highlight the preset in HX Edit you want to overwrite. The script will wait 3 seconds before importing." buttons {"Cancel", "Continue"} default button "Continue"
		delay 3
		tell application "HX Edit" to activate
		delay 0.5
	end if

	tell application "System Events"
		tell process "HX Edit"
			set frontmost to true

			if manualMode is false then
				try
					set presetColumn to outline 1 of scroll area 1 of group 1 of splitter group 1 of window 1
					set firstCell to UI element 1 of row 1 of presetColumn
					perform action "AXPress" of firstCell
				on error
					click at {120, 200}
				end try
				delay 0.2
				key code 126 using {command down} -- Command + Up Arrow to jump to top
				delay 0.2
				key code 36 -- Enter to focus the slot
				delay 0.3
			end if

			keystroke "i" using {command down} -- Import Preset…
			delay 1.5

			keystroke "g" using {command down, shift down} -- Go to Folder
			delay 0.3
			keystroke presetPath
			delay 0.3
			key code 36 -- confirm the path entry
			delay 0.5
			key code 36 -- close the sheet
			delay 0.8
			key code 36 -- choose the file
			delay 1.0
			key code 36 -- confirm the import
		end tell
	end tell
end run

on run
	display dialog "Drag archive files or split volumes onto MacUnpack.app to extract them." buttons {"OK"} default button "OK"
end run

on open droppedItems
	set appPath to POSIX path of (path to me)
	set unpackScript to appPath & "Contents/Resources/mac-unpack.py"
	
	set quotedPaths to {}
	repeat with itemRef in droppedItems
		set end of quotedPaths to quoted form of POSIX path of itemRef
	end repeat
	
	set shellBody to "export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin; " & ¬
		"/usr/bin/python3 " & quoted form of unpackScript & " --recursive " & my joinText(quotedPaths, " ") & "; " & ¬
		"rc=$?; echo; " & ¬
		"if [ $rc -eq 0 ]; then echo 'Extraction finished.'; else echo 'Extraction failed with status' $rc; fi; " & ¬
		"echo; read -n 1 -s -r -p 'Press any key to close...'; exit $rc"
	
	set terminalCommand to "/bin/bash -lc " & quoted form of shellBody
	tell application "Terminal"
		activate
		do script terminalCommand
	end tell
end open

on joinText(itemsList, delimiter)
	set oldDelimiters to AppleScript's text item delimiters
	set AppleScript's text item delimiters to delimiter
	set joinedText to itemsList as text
	set AppleScript's text item delimiters to oldDelimiters
	return joinedText
end joinText

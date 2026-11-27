' Runs the given command line with no window. The autostart task uses this so
' powershell.exe never flashes a console every 15 minutes.
' Usage: wscript.exe //B //Nologo Run-Hidden.vbs <program> [arguments...]
Dim command, index, argument
For index = 0 To WScript.Arguments.Count - 1
    argument = WScript.Arguments(index)
    If InStr(argument, " ") > 0 Then argument = """" & argument & """"
    command = command & argument & " "
Next
If Len(command) = 0 Then WScript.Quit 1
WScript.Quit CreateObject("WScript.Shell").Run(Trim(command), 0, True)

@echo off
wmic process where "name='python.exe' and commandline like '%%outlook_register_concurrent%%'" get processid /value 2>nul

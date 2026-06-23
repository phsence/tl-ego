@echo off
REM TL-Ego Server Launcher for nssm Windows Service
REM Sets up Python path before launching the server

set PYTHONPATH=C:\Users\Administrator\.openclaw\skills\neural_grid;C:\Users\Administrator\.openclaw\workspace
cd /d C:\Users\Administrator\.openclaw\workspace\projects\tl-ego
H:\unsloth_env\Scripts\python.exe tl_ego_server.py

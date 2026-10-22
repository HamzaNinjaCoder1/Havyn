@echo off
rem Production launcher for Windows Task Scheduler.
rem Uses OneDrive for a morning-visible upload and failure notification.
set HAVYN_UPLOAD_DIR=%USERPROFILE%\OneDrive\HavynUploads\nightly
set HAVYN_NOTIFY_FILE=%USERPROFILE%\OneDrive\HavynUploads\nightly-notify.log
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0havyn-nightly.ps1"

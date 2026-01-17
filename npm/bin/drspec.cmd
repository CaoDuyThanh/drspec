@echo off
:: DrSpec wrapper script for Windows
:: This script is invoked by npm and executes the downloaded binary

"%~dp0drspec.exe" %*

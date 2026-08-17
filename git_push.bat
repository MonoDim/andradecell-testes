@echo off
title Enviar Atualizacao para o GitHub - AndradeCell
echo ========================================================
echo   AndradeCell - Enviar Nova Versao para o GitHub
echo ========================================================
echo.

git status

echo.
echo Adicionando arquivos e salvando alteracoes...
git add .

set "commit_msg=Atualizacao do aplicativo AndradeCell"
set /p "commit_input=Digite uma descricao da atualizacao (ou aperte ENTER para padrao): "
if not "%commit_input%"=="" set "commit_msg=%commit_input%"

git commit -m "%commit_msg%"

echo.
echo Enviando para o GitHub...
git push -u origin main

echo.
echo ========================================================
echo   Processo concluido! O aplicativo detectara a versao.
echo ========================================================
pause

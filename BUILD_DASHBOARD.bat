docker login

REM Tag = short git SHA ("-dirty" appended if uncommitted changes are baked in).
for /f %%i in ('git describe --always --dirty') do set GIT_SHA=%%i

docker build --no-cache -f Dockerfile.dashboard --build-arg GIT_SHA=%GIT_SHA% -t deadentropy/mpc-dashboard:%GIT_SHA% -t deadentropy/mpc-dashboard:latest .
docker push deadentropy/mpc-dashboard:%GIT_SHA%
docker push deadentropy/mpc-dashboard:latest

echo "Build Complete"
pause
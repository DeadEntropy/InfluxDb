docker login

REM Tag = short git SHA ("-dirty" appended if uncommitted changes are baked in).
for /f %%i in ('git describe --always --dirty') do set GIT_SHA=%%i

docker build --no-cache -f Dockerfile.mpc --build-arg GIT_SHA=%GIT_SHA% -t deadentropy/mpc-thermal:%GIT_SHA% -t deadentropy/mpc-thermal:latest .
docker push deadentropy/mpc-thermal:%GIT_SHA%
docker push deadentropy/mpc-thermal:latest

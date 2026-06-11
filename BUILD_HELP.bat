docker login
docker build --no-cache -f Dockerfile.mpc -t mpc-thermal .
docker tag mpc-thermal deadentropy/mpc-thermal:latest
docker push deadentropy/mpc-thermal:latest

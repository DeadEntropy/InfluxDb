docker login
docker build --no-cache -f Dockerfile.shadow -t mpc-shadow .
docker tag mpc-shadow deadentropy/mpc-shadow:latest
docker push deadentropy/mpc-shadow:latest

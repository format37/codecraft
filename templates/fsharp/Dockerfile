# Official .NET client on the .NET 5 SDK image: the 2020 Dockerfile starts from debian:buster, whose
# apt repositories are gone, and installs the same SDK with dotnet-install.sh.
FROM mcr.microsoft.com/dotnet/sdk:5.0
COPY client /project
WORKDIR /project
RUN dotnet build -c Release

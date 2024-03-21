# The Ultimate Plex Stack!

Welcome to my Plex stack repository! This repository showcases my Docker Compose setup for managing various media-related services using Docker containers. The compose file is meant to be changed to each users liking as I know not everyone has the same requirements. Hope you enjoy!

## Overview

This Plex Stack includes the following services:

- **Plex:** Media server for streaming movies and TV shows.
- **Radarr:** Movie management and automation.
- **Sonarr:** TV show management and automation.
- **Prowlarr:** Indexer manager for Radarr and Sonarr.
- **Overseerr:** Request management and monitoring for Plex.
- **Gluetun:** VPN container with WireGuard support for secure browsing.
- **Qbittorrent:** BitTorrent client with VPN support.
- **De-unhealth:** Monitors VPN health and restarts Qbittorrent if necessary.
- **Bazarr:** Subtitle management for movies and TV shows.
- **Flaresolverr:** Used as a proxy server to bypass Cloudflare and DDoS-GUARD protection.
- **Dozzle:** Used to view the logs of any container.
- **Wizarr:** Used to create links that can be sent to users so they can be invited to your media server.
- **Plex Auto Lanaguages:** Used to auto update the language of your Plex Tv episodes

<img width="353" alt="image" src="https://github.com/DonMcD/ultimate-plex-stack/assets/90471623/a9b8faf8-072e-4fb3-ac18-2ffd29d0f760">


## Dependencies

1. Linux
2. Docker / Docker Compose
3. OPTIONAL: Portainer - Docker GUI

## How to Use

1. Clone this repository / Copy the docker-compose.yml file:

   ```bash
   git clone https://github.com/DonMcD/ultimate-plex-stack.git
2. Fill in the required details such as the environment variables
3. OPTIONAL: Setup a reverse proxy so you can use radarr.my-domain.com instead of 192.168.1.10 to access each of your apps

## Example of Environment variables in Portainer
<img width="657" alt="image" src="https://github.com/DonMcD/ultimate-plex-stack/assets/90471623/9a614eb0-8ff7-4eb9-b154-61c08cd595e9">

  
File location examples:
- {UNRAID_SHARE} = /share
- {BASE_PATH} = /home/username/docker

To allow hardlinking to work (which you will definitely want!) you will have to use the same root folder in all of your container path. In this example we use "/share", so in the container it will look like "/share/downloads/tv"

An example of my folder structure:  
![image](https://github.com/DonMcD/ultimate-plex-stack/assets/90471623/2003ac26-a929-4ff6-ad67-e35fc51fb51a)
  
- Feel free to expand your folders to also include "books" or "music" as you need for your setup
  

  
1. In Radarr you will want to set your category to "movies", this will create the movies folder
2. In Sonarr you will want to set your category to "tv", this will create the tv folder

  
Anytime you reference your media folder in a container you want the path to look like /share/media/tv instead of /tv like a lot of the default guides say, if you do end up mapping the path as /tv hardlinking will not work

## Possible Additions

1. Organizr - Creates a lovely dashboard to help navigate to all of your apps
2. Portainer - Docker GUI
3. UptimeKuma - Gives you the ability to monitor your services


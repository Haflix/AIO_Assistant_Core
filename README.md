# AIO_Assistant_Core
This is the core of a Pluginloader. It's supposed to make connecting scripts (synchronous & asynchronous!) easier across multiple devices that can communicate with each other (networking can be disabled in the config). 

The important functionalities are:

- Pluginloading (They are hot-pluggable)
- Easy syntax for creating a plugin 
    -> For example a one-liner for executing a call/request to another plugin
- Easy connection between synchronous and asynchronous plugins



## Feature Documentation (Will be done later! Its not representative as of now!!!)

- [Explanation of documentation](docs/docs_structure/README.md)
- [PluginCore](docs/features/PluginCore.md)



## Future-Plans
These are just ideas that seem cool to me and are not yet implemented at all. They MAY be implemented (no guarantee tho)

- Streams of data (e.g. yield etc)
- Caching of locations of plugins on remote devices
- Security

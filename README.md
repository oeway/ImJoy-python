![ENGINE_VERSION](https://img.shields.io/badge/dynamic/json.svg?color=success&label=imjoy%20engine&prefix=v&query=version&url=https%3A%2F%2Fraw.githubusercontent.com%2Foeway%2FImJoy-Engine%2Fmaster%2Fimjoy%2FVERSION) ![API_VERSION](https://img.shields.io/badge/dynamic/json.svg?color=success&label=api%20version&prefix=v&query=api_version&url=https%3A%2F%2Fraw.githubusercontent.com%2Foeway%2FImJoy-Engine%2Fmaster%2Fimjoy%2FVERSION) ![PyPI](https://img.shields.io/pypi/v/imjoy.svg?style=popout) ![GitHub](https://img.shields.io/github/license/oeway/ImJoy-Engine.svg) [![Build Status](https://travis-ci.com/oeway/ImJoy-Engine.svg?branch=master)](https://travis-ci.com/oeway/ImJoy-Engine) [![ImJoy Version](https://img.shields.io/badge/dynamic/json.svg?color=success&label=imjoy&prefix=v&query=version&url=https://raw.githubusercontent.com/oeway/ImJoy/master/web/package.json)](https://imjoy.io/#/app)
# ImJoy Plugin Engine
The plugin engine used for running python plugins in ImJoy (https://imjoy.io).

This library is mainly based on jupyter notebook server, with additonal libraries and convenient settings.

## Installation
* If you don't have a conda environemnt, download and install [Miniconda with Python 3.7](https://conda.io/miniconda.html) (or [Anaconda with Python 3.7](https://www.anaconda.com/download/) if you prefer a full installation).

* Start a **Terminal**(Mac and Linux) or **Anaconda Prompt**(Windows), then run the following command:

```
pip install -U [jupyter]
```

The above command will also install jupyter notebook and [imjoy-elfinder](https://github.com/imjoy-team/imjoy-elfinder).

* If you encountered any error related to `git` or `pip`, try to run : `conda install -y git pip` before the above command. (Otherwise, please check **FAQs**.)

## Usage

To use it after the installation:
* Run `imjoy --jupyter` command in a **Terminal** or **Anaconda Prompt**, and keep the window running.

* Go to https://imjoy.io, connect to the plugin engine. For the first time, you will be asked to fill in a token generated by the plugin engine from the previous step. Once connected, you can start to run python plugins through the plugin engine.


Please note that if you are trying to use the ImJoy Plugin Engine running on a remote server, please use the ImJoy web App served on your server (`http://YOUR_REMOTE_IP:9527`) instead of `https://imjoy.io`. This is because most browsers do not allow a web application served through `https` to connect to an unsecured server (your remote server). Alternatively, you can use a proxy to enable `https` for the plugin engine. Then you will be able to use it with `https://imjoy.io`.


## More details and FAQs in [Docs](https://imjoy.io/docs/#/user_manual)

# Roadmap
You can track the progress of the project here: https://github.com/oeway/ImJoy/projects/2

# Bug report and feature request

Please submit your issue to [ImJoy/issues ](https://github.com/oeway/ImJoy/issues)

## Development

- Development requires Python 3.6, since we use [`black`](https://github.com/ambv/black) for code formatting.

```
  git clone git@github.com:oeway/ImJoy-Engine.git
  # Enter directory.
  cd ImJoy-Engine
  # Install all development requirements and package in development mode.
  pip3 install -r requirements_dev.txt
```

- Run `tox` to run all tests and lint, including checking that `black` doesn't change any files.

""" Juju helpers
"""
import asyncio
import inspect
import json
import os
from concurrent import futures
from functools import partial, wraps
from pathlib import Path
from pprint import pformat
from subprocess import DEVNULL, PIPE, CalledProcessError, Popen, TimeoutExpired

import yaml
from juju.model import Model

from bundleplacer.charmstore_api import CharmStoreID
from conjureup import async as cu_async, consts
from conjureup.app_config import app
from conjureup.utils import error, is_linux, juju_path, run

JUJU_ASYNC_QUEUE = "juju-async-queue"

PENDING_DEPLOYS = cu_async.Counter()


class ControllerNotFoundException(Exception):
    "An error when a controller can't be found in juju's config"


# login decorator
def requires_login(f):
    def _decorator(*args, **kwargs):
        if not app.juju.authenticated:
            login(force=True)
        return f(*args, **kwargs)
    return wraps(f)(_decorator)


def read_config(name):
    """ Reads a juju config file

    Arguments:
    name: filename without extension (ext defaults to yaml)

    Returns:
    dictionary of yaml object
    """
    abs_path = os.path.join(juju_path(), "{}.yaml".format(name))
    if not os.path.isfile(abs_path):
        raise Exception("Cannot load {}".format(abs_path))
    return yaml.safe_load(open(abs_path))


def get_bootstrap_config(controller_name):
    try:
        bootstrap_config = read_config("bootstrap-config")
    except Exception:
        # We may be trying to access the bootstrap-config to quickly
        # between the time of juju bootstrap occurs and this function
        # is accessed.
        app.log.exception("Could not load bootstrap-config, "
                          "setting an empty controllers dict.")
        bootstrap_config = dict(controllers={})
    if 'controllers' not in bootstrap_config:
        raise Exception("Could not read Juju's bootstrap-config.yaml")
    cd = bootstrap_config['controllers'].get(controller_name, None)
    if cd is None:
        raise ControllerNotFoundException(
            "'{}' not found in Juju's "
            "bootstrap-config.yaml".format(controller_name))
    return cd


def get_current_controller():
    """ Grabs the current default controller
    """
    try:
        return get_controllers()['current-controller']
    except KeyError:
        return None


def get_controller(id):
    """ Return specific controller

    Arguments:
    id: controller id
    """
    if 'controllers' in get_controllers() \
       and id in get_controllers()['controllers']:
        return get_controllers()['controllers'][id]
    return None


def get_controller_in_cloud(cloud):
    """ Returns a controller that is bootstrapped on the named cloud

    Arguments:
    cloud: cloud to check for

    Returns:
    available controller or None if nothing available
    """
    controllers = get_controllers()['controllers'].items()
    for controller_name, controller in controllers:
        if cloud == controller['cloud']:
            return controller_name
    return None


def _handle_exception(ex, exc_cb=None):
    if isinstance(ex, asyncio.CancelledError):
        return
    if exc_cb:
        try:
            raise ex
        except:
            app.log.exception(str(ex))
        try:
            exc_cb(ex)
        except SystemExit:
            cu_async.ShutdownEvent.set()
    elif app.headless:
        app.log.exception(str(ex))
        error(str(ex))
        cu_async.ShutdownEvent.set()
    else:
        app.ui.show_exception_message(ex)


def login(force=False):
    """ Login to Juju API server
    """
    if app.juju.authenticated and not force:
        return

    if app.current_controller is None:
        raise Exception("Unable to determine current controller")

    if app.current_model is None:
        raise Exception("Tried to login with no current model set.")

    loop = asyncio.new_event_loop()
    app.juju.client = Model(loop)
    model_name = '{}:{}'.format(app.current_controller,
                                app.current_model)

    async def _model_connect():
        app.log.info('Connecting to model {}...'.format(model_name))
        await app.juju.client.connect_model(model_name)
        app.log.info('Connected')

    async def _shutdown_watcher():
        try:
            while not cu_async.ShutdownEvent.is_set():
                await asyncio.sleep(0.1, loop=loop)
        except SystemExit:
            pass
        for task in asyncio.Task.all_tasks(loop):
            is_juju_task = (
                inspect.iscoroutine(task._coro) and
                task._coro.cr_frame and
                inspect.getmodule(task._coro.cr_frame).__name__ == __name__ and
                task._coro.__name__ != '_shutdown_watcher')
            if is_juju_task:
                task.cancel()
        app.log.info('Disconnecting model')
        await app.juju.client.disconnect()
        app.log.info('Disconnected')

    # this has to be run in its own queue / thread because run_until_complete
    # won't return until ShutdownEvent.set() is called
    cu_async.submit(partial(loop.run_until_complete,
                            _shutdown_watcher()),
                    _handle_exception,
                    queue_name='libjuju')
    f = asyncio.run_coroutine_threadsafe(_model_connect(), loop)
    app.juju.authenticated = True
    f.result()  # block until connected


def bootstrap(controller, cloud, model='conjure-up', series="xenial",
              credential=None):
    """ Performs juju bootstrap

    If not LXD pass along the newly defined credentials

    Arguments:
    controller: name of your controller
    cloud: name of local or public cloud to deploy to
    series: define the bootstrap series defaults to xenial
    log: application logger
    credential: credentials key
    """
    if app.current_region is not None:
        app.log.debug("Bootstrapping to set region: {}")
        cloud = "{}/{}".format(app.current_cloud, app.current_region)
    cmd = "juju bootstrap {} {} " \
          "--config image-stream=daily ".format(
              cloud, controller)
    cmd += "--config enable-os-upgrade=false "
    cmd += "--default-model {} ".format(model)
    if app.argv.http_proxy:
        cmd += "--config http-proxy={} ".format(app.argv.http_proxy)
    if app.argv.https_proxy:
        cmd += "--config https-proxy={} ".format(app.argv.https_proxy)
    if app.argv.apt_http_proxy:
        cmd += "--config apt-http-proxy={} ".format(app.argv.apt_http_proxy)
    if app.argv.apt_https_proxy:
        cmd += "--config apt-https-proxy={} ".format(app.argv.apt_https_proxy)
    if app.argv.no_proxy:
        cmd += "--config no-proxy={} ".format(app.argv.no_proxy)
    if app.argv.bootstrap_timeout:
        cmd += "--config bootstrap-timeout={} ".format(
            app.argv.bootstrap_timeout)
    if app.argv.bootstrap_to:
        cmd += "--to {} ".format(app.argv.bootstrap_to)

    cmd += "--bootstrap-series={} ".format(series)
    if credential is not None:
        cmd += "--credential {} ".format(credential)

    if app.argv.debug:
        cmd += "--debug"
    app.log.debug("bootstrap cmd: {}".format(cmd))

    try:
        pathbase = os.path.join(app.config['spell-dir'],
                                '{}-bootstrap').format(app.current_controller)
        with open(pathbase + ".out", 'w') as outf:
            with open(pathbase + ".err", 'w') as errf:
                p = Popen(cmd, shell=True, stdout=outf,
                          stderr=errf)
                while p.poll() is None:
                    cu_async.sleep_until(2)
                return p
    except CalledProcessError:
        raise Exception("Unable to bootstrap.")
    except cu_async.ThreadCancelledException:
        p.terminate()
        try:
            p.wait(timeout=2)
        except TimeoutExpired:
            p.kill()
            p.wait()
        return p
    except Exception as e:
        raise e


def bootstrap_async(controller, cloud, model='conjure-up', credential=None,
                    exc_cb=None):
    """ Performs a bootstrap asynchronously
    """
    return cu_async.submit(partial(bootstrap,
                                   controller=controller,
                                   cloud=cloud,
                                   model=model,
                                   credential=credential), exc_cb,
                           queue_name=JUJU_ASYNC_QUEUE)


def has_jaas_auth():
    oauth_token = Path('~/.local/share/juju/store-usso-token').expanduser()
    go_cookies = Path('~/.go-cookies').expanduser()
    if oauth_token.exists():
        return True
    if go_cookies.exists():
        go_cookies = json.loads(go_cookies.read_text())
        for cookie in go_cookies or []:
            if cookie['Domain'] == consts.JAAS_DOMAIN:
                return True
    return False


def register_controller(name, endpoint, email, password, twofa, timeout=30,
                        cb=None, fail_cb=None, timeout_cb=None, exc_cb=None):
    async def _register_controller():
        try:
            proc = await asyncio.create_subprocess_exec(
                'juju', 'register', '-B', endpoint,
                stdin=PIPE, stdout=PIPE, stderr=PIPE,
            )
            if has_jaas_auth():
                # if the user already authed with jujucharms.com, such as by
                # logging in with the charm command, or registering JaaS and
                # then unregistering it, we only need to name the controller
                input = [name]
            else:
                input = [email, password, twofa, name]
            try:
                stdin = b''.join(b'%s\n' % bytes(f, 'utf8') for f in input)
                (stdout, stderr) = await asyncio.wait_for(
                    proc.communicate(stdin), timeout)
            except asyncio.TimeoutError:
                proc.kill()
                if timeout_cb:
                    timeout_cb()
                elif fail_cb:
                    fail_cb((proc.stderr or b'').decode('utf8'))
                return
            if proc.returncode > 0:
                if fail_cb:
                    fail_cb(stderr.decode('utf8'))
                    return
                else:
                    raise CalledProcessError(stderr.decode('utf8'))
            cb()
        except Exception as e:
            if exc_cb:
                exc_cb(e)
                return
            raise
    asyncio.get_event_loop().create_task(_register_controller())


def model_available():
    """ Checks if juju is available

    Returns:
    True/False if juju status was successful and a working model is found
    """
    try:
        run('juju status -m {}:{}'.format(app.current_controller,
                                          app.current_model),
            shell=True,
            check=True,
            stderr=DEVNULL,
            stdout=DEVNULL)
    except CalledProcessError:
        return False
    return True


def autoload_credentials():
    """ Automatically checks known places for cloud credentials
    """
    try:
        run('juju autoload-credentials', shell=True, check=True)
    except CalledProcessError:
        return False
    return True


def get_credential(cloud, user):
    """ Get credentials for user

    Arguments:
    cloud: cloud applicable to user credentials
    user: user listed in the credentials
    """
    creds = get_credentials()
    if cloud in creds.keys():
        if user in creds[cloud].keys():
            return creds[cloud][user]
    raise Exception(
        "Unable to locate credentials for: {}".format(user))


def get_credentials(secrets=True):
    """ List credentials

    This will fallback to reading the credentials file directly

    Arguments:
    secrets: True/False whether to show secrets (ie password)

    Returns:
    List of credentials
    """
    cmd = 'juju list-credentials --format yaml'
    if secrets:
        cmd += ' --show-secrets'
    sh = run(cmd, shell=True, stdout=PIPE, stderr=PIPE)
    if sh.returncode > 0:
        try:
            env = read_config('credentials')
            return env['credentials']
        except:
            raise Exception(
                "Unable to list credentials: {}".format(
                    sh.stderr.decode('utf8')))
    env = yaml.safe_load(sh.stdout.decode('utf8'))
    return env['credentials']


def get_regions(cloud):
    """ List available regions for cloud

    Arguments:
    cloud: Cloud to list regions for

    Returns:
    Dictionary of all known regions for cloud
    """
    sh = run('juju list-regions {} --format yaml'.format(cloud),
             shell=True, stdout=PIPE, stderr=PIPE)
    if sh.returncode > 0:
        raise Exception(
            "Unable to list regions: {}".format(sh.stderr.decode('utf8'))
        )
    return yaml.safe_load(sh.stdout.decode('utf8'))


def get_clouds():
    """ List available clouds

    Returns:
    Dictionary of all known clouds including newly created MAAS/Local
    """
    sh = run('juju list-clouds --format yaml',
             shell=True, stdout=PIPE, stderr=PIPE)
    if sh.returncode > 0:
        raise Exception(
            "Unable to list clouds: {}".format(sh.stderr.decode('utf8'))
        )
    return yaml.safe_load(sh.stdout.decode('utf8'))


def get_compatible_clouds(cloud_types=None):
    """ List cloud types compatible with the current spell and controller.

    Arguments:
    clouds: optional initial list of clouds to filter
    Returns:
    List of cloud types
    """
    clouds = get_clouds()
    cloud_types = set(cloud_types or (c['type'] for c in clouds.values()))

    if 'lxd' in cloud_types:
        # normalize 'lxd' cloud type to localhost; 'lxd' can happen
        # depending on how the controller was bootstrapped
        cloud_types -= {'lxd'}
        cloud_types |= {'localhost'}

    if not is_linux():
        # LXD not available on macOS
        cloud_types -= {'localhost'}

    if app.current_controller:
        # if we already have a controller, we should query
        # it via the API for what clouds it supports; for now,
        # though, just assume it's JAAS and hard-code the options
        cloud_types &= consts.JAAS_CLOUDS

    whitelist = set(app.config['metadata'].get('cloud-whitelist', []))
    blacklist = set(app.config['metadata'].get('cloud-blacklist', []))
    if len(whitelist) > 0:
        return sorted(cloud_types & whitelist)

    elif len(blacklist) > 0:
        return sorted(cloud_types ^ blacklist)

    return sorted(cloud_types)


def get_cloud_types_by_name():
    """ Return a mapping of cloud names to their type.

    This accounts for some normalizations that get_clouds() doesn't.
    """
    clouds = {n: c['type'] for n, c in get_clouds().items()}

    # normalize 'lxd' cloud type to localhost; 'lxd' can happen
    # depending on how the controller was bootstrapped
    for name, cloud_type in clouds.items():
        if cloud_type == 'lxd':
            clouds[name] = 'localhost'

    return clouds


def get_cloud(name):
    """ Return specific cloud information

    Arguments:
    name: name of cloud to query, ie. aws, lxd, local:provider
    Returns:
    Dictionary of cloud attributes
    """
    if name in get_clouds().keys():
        return get_clouds()[name]
    raise LookupError("Unable to locate cloud: {}".format(name))


def constraints_to_dict(constraints):
    """Parses a constraint string into a dict. Does not do unit
    conversion. Expects root-disk, mem and cores to be int values, and
    root-disk and mem should be in megabytes."""
    new_constraints = {}
    if not isinstance(constraints, str):
        app.log.debug(
            "Invalid constraints: {}, skipping".format(
                constraints))
        return new_constraints

    list_constraints = [c for c in constraints.split(' ')
                        if c != ""]
    for c in list_constraints:
        try:
            constraint, value = c.split('=')
            if constraint in ['tags', 'spaces']:
                value = value.split(',')
            elif constraint in ['root-disk', 'mem', 'cores']:
                value = int(value)
            else:
                raise Exception(
                    "Unsupported constraint: {}".format(constraint))
            new_constraints[constraint] = value
        except ValueError as e:
            app.log.debug("Skipping constraint: {} ({})".format(c, e))
    return new_constraints


def constraints_from_dict(cdict):
    return " ".join(["{}={}".format(k, v) for k, v in cdict.items()])


def deploy(bundle):
    """ Juju deploy bundle

    Arguments:
    bundle: Name of bundle to deploy, can be a path to local bundle file or
            charmstore path.
    """
    try:
        return run('juju deploy {}'.format(bundle), shell=True,
                   stdout=DEVNULL, stderr=PIPE)
    except CalledProcessError as e:
        raise e


@requires_login
def add_machines(machines, msg_cb=None, exc_cb=None):
    """Add machines to model

    Arguments:

    machines: list of dictionaries of machine attributes.
    The key 'series' is required, and 'constraints' is the only other
    supported key

    """
    if not machines:
        return
    elif len(machines) > 1:
        pl = "s"
    else:
        pl = ""

    for machine in machines:
        constraints = machine.get('constraints', '')
        machine['constraints'] = constraints_to_dict(constraints)

    async def _add_machines():
        msg = "Adding machine{}: {}".format(
            pl, [(m['series'], m['constraints']) for m in machines])
        app.log.info(msg)
        if msg_cb:
            msg_cb(msg)
        done, pending = await asyncio.wait(
            [app.juju.client.add_machine(series=m['series'],
                                         constraints=m['constraints'])
             for m in machines],
            loop=app.juju.client.loop)
        exceptions = [f.exception() for f in done if f.exception()]
        ids = [f.result().id for f in done if not f.exception()]
        if exceptions:
            _handle_exception(exceptions[0], exc_cb)
        if ids:
            msg = "Added machine{}: {}".format(pl, ids)
            app.log.info(msg)
            if msg_cb:
                msg_cb(msg)
        return ids

    return asyncio.run_coroutine_threadsafe(_add_machines(),
                                            app.juju.client.loop)


@requires_login
def deploy_service(service, default_series, msg_cb=None, exc_cb=None):
    """Juju deploy service.

    If the service's charm ID doesn't have a revno, will query charm
    store to get latest revno for the charm.

    If the service's charm ID has a series, use that, otherwise use
    the provided default series.

    Arguments:
    service: Service to deploy
    msg_cb: message callback
    exc_cb: exception handler callback

    Returns a future that will be completed after the deploy has been
    submitted to juju

    """

    async def _deploy_async():
        if service.csid.rev == "":
            id_no_rev = service.csid.as_str_without_rev()
            mc = app.metadata_controller
            futures.wait([mc.metadata_future])
            info = mc.get_charm_info(id_no_rev, lambda _: None)
            service.csid = CharmStoreID(info["Id"])

        deploy_args = {}
        try:
            deploy_args = dict(
                entity_url=service.csid.as_str(),
                application_name=service.service_name,
                num_units=service.num_units,
                constraints=service.constraints,
                to=service.placement_spec,
                config=service.options,
            )
            msg = 'Deploying {}...'.format(service.service_name)
            app.log.info(msg)
            app.log.debug(pformat(deploy_args))
            if msg_cb:
                msg_cb(msg)
            app_inst = await app.juju.client.deploy(**deploy_args)
            msg = '{}: deployed, installing.'.format(service.service_name)
            app.log.info(msg)
            if msg_cb:
                msg_cb(msg)

            if service.expose:
                await app_inst.expose()

            PENDING_DEPLOYS.decrement()
        except asyncio.CancelledError:
            return
        except Exception:
            app.log.exception('Error deploying {}:\n{}'.format(
                service.service_name,
                pformat(deploy_args),
            ))
            raise

    PENDING_DEPLOYS.increment()
    f = asyncio.run_coroutine_threadsafe(_deploy_async(),
                                         app.juju.client.loop)
    f.add_done_callback(lambda f: (f.exception() and
                                   _handle_exception(f.exception(), exc_cb)))
    return f


@requires_login
def set_relations(services, msg_cb=None, exc_cb=None):
    """ Juju set relations

    Arguments:
    services: list of services with relations to set
    msg_cb: message callback
    exc_cb: exception handler callback
    """
    relations = set()
    for service in services:
        for a, b in service.relations:
            if (a, b) not in relations and (b, a) not in relations:
                relations.add((a, b))

    async def do_add_all():
        await app.juju.client.block_until(PENDING_DEPLOYS.empty)
        if msg_cb:
            msg_cb("Setting application relations")

        for a, b in relations:
            try:
                await app.juju.client.add_relation(a, b)
            except Exception as e:
                _handle_exception(e, exc_cb)
                return
        if msg_cb:
            msg_cb("Completed setting application relations")

    return asyncio.run_coroutine_threadsafe(do_add_all(),
                                            app.juju.client.loop)


def get_controller_info(name=None):
    """ Returns information on current controller

    Arguments:
    name: if set shows info controller, otherwise displays current.
    """
    cmd = 'juju show-controller --format yaml'
    if name is not None:
        cmd += ' {}'.format(name)
    sh = run(cmd, shell=True, stdout=PIPE, stderr=PIPE)
    if sh.returncode > 0:
        raise Exception(
            "Unable to determine controller: {}".format(
                sh.stderr.decode('utf8')))
    out = yaml.safe_load(sh.stdout.decode('utf8'))
    try:
        return next(iter(out.values()))
    except:
        return out


def get_controllers():
    """ List available controllers

    Returns:
    List of known controllers
    """
    sh = run('juju list-controllers --format yaml',
             shell=True, stdout=PIPE, stderr=PIPE)
    if sh.returncode > 0:
        raise LookupError(
            "Unable to list controllers: {}".format(sh.stderr.decode('utf8')))
    env = yaml.safe_load(sh.stdout.decode('utf8'))
    return env


def get_account(controller):
    """ List account information for controller

    Arguments:
    controller: controller id

    Returns:
    Dictionary containing list of accounts for controller and the
    current account in use.

    """
    return get_accounts().get(controller, {})


def get_accounts():
    """ List available accounts

    Returns:
    List of known accounts
    """
    env = os.path.join(juju_path(), 'accounts.yaml')
    if not os.path.isfile(env):
        raise Exception(
            "Unable to find: {}".format(env))
    with open(env, 'r') as c:
        env = yaml.load(c)
        return env['controllers']
    raise Exception("Unable to find accounts")


def get_model(controller, name):
    """ List information for model

    Arguments:
    name: model name
    controller: name of controller to work in

    Returns:
    Dictionary of model information
    """
    models = get_models(controller)['models']
    for m in models:
        if m['name'] == name:
            return m
    raise LookupError(
        "Unable to find model: {}".format(name))


def add_model(name, controller, cloud, allow_exists=False):
    """ Adds a model to current controller

    Arguments:
    controller: controller to add model in
    allow_exists: re-use an existing model, if one exists.
    """
    if allow_exists and model_available():
        return

    sh = run('juju add-model {} -c {} {}'.format(name, controller, cloud),
             shell=True, stdout=DEVNULL, stderr=PIPE)
    if sh.returncode > 0:
        raise Exception(
            "Unable to create model: {}".format(sh.stderr.decode('utf8')))
    # the CLI has to connect to the model at least once to populate the model
    # macaroons; model_available does this and verifies the model is working
    if not model_available():
        raise Exception("Unable to connect model after creation")


def add_model_async(name, controller, cloud, exc_cb=None):
    return cu_async.submit(partial(add_model, name, controller, cloud),
                           exc_cb, queue_name=JUJU_ASYNC_QUEUE)


def destroy_model_async(controller, model, exc_cb=None):
    """ Destroys a model async
    """
    return cu_async.submit(partial(destroy_model,
                                   controller=controller,
                                   model=model),
                           exc_cb,
                           queue_name=JUJU_ASYNC_QUEUE)


def destroy_model(controller, model):
    """ Destroys a model within a controller

    Arguments:
    controller: name of controller
    model: name of model to destroy
    """
    sh = run('juju destroy-model -y {}:{}'.format(controller, model),
             shell=True, stdout=DEVNULL, stderr=PIPE)
    if sh.returncode > 0:
        raise Exception(
            "Unable to destroy model: {}".format(sh.stderr.decode('utf8')))


def get_models(controller):
    """ List available models

    Arguments:
    controller: existing controller to get models for

    Returns:
    List of known models
    """
    sh = run('juju list-models --format yaml -c {}'.format(controller),
             shell=True, stdout=PIPE, stderr=PIPE)
    if sh.returncode > 0:
        raise LookupError(
            "Unable to list models: {}".format(sh.stderr.decode('utf8')))
    out = yaml.safe_load(sh.stdout.decode('utf8'))
    return out


def get_current_model():
    try:
        return get_models()['current-model']
    except:
        return None


def version():
    """ Returns version of Juju
    """
    sh = run('juju version', shell=True, stdout=PIPE, stderr=PIPE)
    if sh.returncode > 0:
        raise Exception(
            "Unable to get Juju Version".format(sh.stderr.decode('utf8')))
    out = sh.stdout.decode('utf8')
    if isinstance(out, list):
        return out.pop()
    else:
        return out

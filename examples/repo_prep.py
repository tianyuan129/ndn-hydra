import logging, os, asyncio, base64
import sys
from argparse import ArgumentParser, SUPPRESS
from datetime import datetime, timedelta
from typing import List
# NDN Imports
from ndn.encoding import Name
from ndn.app import NDNApp
from ndn.utils import timestamp
from ndn.security.tpm import TpmFile
import ndn.encoding as enc
import ndn.app_support.security_v2 as sv2
import ndn.app_support.light_versec.checker as chk
import ndn.app_support.light_versec.compiler as cpl
# Custom Imports
from envelope.impl.storage import Sqlite3Box
from envelope.impl import EnvelopeImpl

logging.basicConfig(format='[{asctime}]{levelname}:{message}',
                    datefmt='%Y-%m-%d %H:%M:%S',
                    level=logging.DEBUG,
                    style='{')

app = NDNApp()
async def prepare_keys(groupprefix, nodeids: List):
    # /hydra/node/n1/hydra/group/data/1
    lvs_text = r'''
    #KEY: "KEY"/_/_/_
    #site: "hydra"
    #nodePrefix: #site/node/_
    #groupSyncInterestSend: #site/sync/_ <= #node
    #groupSyncInterestReceive: #site/sync/_/_ <= #node
    #groupData1: #nodePrefix/#site/group/_ <= #node
    #groupData2: #nodePrefix/#site/group/data/_ <= #node
    #node: #site/node/_/#KEY <= #root
    #root: #site/#KEY
    '''
    basedir = os.path.dirname(os.path.abspath(sys.argv[0]))
    secParamsdir = os.path.join(basedir, 'secParams')
    tpm_path = os.path.join(secParamsdir, 'RepoNodeSecurityPrivKeys')
    os.makedirs(tpm_path, exist_ok=True)

    # cleaning up
    try:
        os.remove(secParamsdir)
    except:
        pass
    security_manager = EnvelopeImpl(app, TpmFile(tpm_path))

    # anchor
    anchor_key_name, anchor_key_pub = security_manager.tpm.generate_key(Name.from_str("/hydra"))
    anchor_self_signer = security_manager.tpm.get_signer(anchor_key_name, None)
    anchor_cert_name, anchor_bytes = sv2.self_sign(anchor_key_name, anchor_key_pub, anchor_self_signer)
    logging.info(enc.Name.to_str(anchor_cert_name))
    chk.DEFAULT_USER_FNS.update(
        {'$eq_any': lambda c, args: any(x == c for x in args)}
    )
    model = cpl.compile_lvs(lvs_text)
    await security_manager.set(anchor_bytes, model, chk.DEFAULT_USER_FNS)
    security_manager.index(anchor_bytes)

    with open(os.path.join(secParamsdir, "anchor.ndncert"), "w") as af:
        af.write(base64.b64encode(anchor_bytes).decode())

    with open(os.path.join(secParamsdir, "model.lvs"), "w") as mf:
        mf.write(base64.b64encode(model.encode()).decode())

    # node
    for node in nodeids:
        node_key_name, node_key_pub = security_manager.tpm.generate_key(Name.from_str("/hydra/node/" + node))
        node_cert_name = node_key_name + [enc.Component.from_str("noc"), enc.Component.from_version(timestamp())]
        node_cert_bytes = security_manager.sign_cert(node_cert_name, enc.MetaInfo(content_type=enc.ContentType.KEY, freshness_period=3600000),
                                                     node_key_pub, datetime.utcnow(), datetime.utcnow() + timedelta(days=10))
        Sqlite3Box.initialize(os.path.join(secParamsdir, f'RepoNodeCerts-{node}.db'))
        node_box = Sqlite3Box(os.path.join(secParamsdir, f'RepoNodeCerts-{node}.db'))
        node_box.put(node_cert_name, node_cert_bytes)

def parse_cmd_args() -> dict:
    # Command Line Parser
    parser = ArgumentParser(add_help=False,description="Prepare security parameters for Secured SVS Chat.")
    requiredArgs = parser.add_argument_group("required arguments")
    optionalArgs = parser.add_argument_group("optional arguments")
    informationArgs = parser.add_argument_group("information arguments")
    # Adding all Command Line Arguments
    requiredArgs.add_argument("-n", "--nodename",action="append",dest="node_name",required=True,help="flat node names")
    optionalArgs.add_argument("-gp","--groupprefix",action="store",dest="group_prefix",required=False,help="overrides config | routable group prefix to listen from")
    informationArgs.add_argument("-h","--help",action="help",default=SUPPRESS,help="show this help message and exit")
    # Getting all Arguments
    argvars = parser.parse_args()
    args = {}
    args["group_prefix"] = argvars.group_prefix if argvars.group_prefix is not None else "/svs"
    args["node_id"] = argvars.node_name
    return args


def main() -> int:
    args = parse_cmd_args()
    asyncio.run(prepare_keys(args["group_prefix"], args["node_id"]))

if __name__ == "__main__":
    sys.exit(main())
# -------------------------------------------------------------
# NDN Hydra MainLoop
# -------------------------------------------------------------
#  @Project: NDN Hydra
#  @Date:    2021-01-25
#  @Authors: Please check AUTHORS.rst
#  @Source-Code:   https://github.com/justincpresley/ndn-hydra
#  @Documentation: https://ndn-hydra.readthedocs.io
#  @Pip-Library:   https://pypi.org/project/ndn-hydra
# -------------------------------------------------------------

import asyncio as aio
import logging, base64
import secrets
import time
import random
from typing import Dict, List
from ndn.app import NDNApp
from ndn.encoding import Name, Component
import ndn.app_support.light_versec.checker as chk
from ndn.app_support.light_versec import LvsModel
import ndn.app_support.security_v2 as sv2
from ndn.security.tpm import TpmFile
from ndn.types import InterestNack, InterestTimeout
from svs import SVSync
from svs.security import SecurityOptions, SigningInfo, ValidatingInfo
from ndn.storage import Storage, SqliteStorage
from ndn_hydra.repo.modules import *
from ndn_hydra.repo.group_messages import *
from ndn_hydra.repo.modules.file_fetcher import FileFetcher
from ndn_hydra.repo.utils.garbage_collector import collect_db_garbage
from ndn_hydra.repo.utils.concurrent_fetcher import concurrent_fetcher
from envelope.impl import EnvelopeImpl
from envelope.impl.storage import Sqlite3Box, ExpressToNetworkBox

class MainLoop:
    def __init__(self, app: NDNApp, config: Dict, global_view: GlobalView, data_storage: Storage, svs_storage: Storage, file_fetcher: FileFetcher, using_envelope = False):
        self.app = app
        self.config = config
        self.global_view = global_view
        self.data_storage = data_storage
        self.svs_storage = svs_storage
        self.file_fetcher = file_fetcher
        self.file_fetcher.store_func = self.store
        self.svs = None
        self.logger = logging.getLogger()
        self.node_name = self.config['node_name']
        self.tracker = HeartbeatTracker(self.node_name, global_view, config['loop_period'], config['heartbeat_rate'], config['tracker_rate'], config['beats_to_fail'], config['beats_to_renew'])
        self.last_garbage_collect_t = time.time() # time in seconds
        self.using_envelope = using_envelope
    async def start(self):
        async def load_envelope(anchor_path, model_path, db_path, tpm_path):
            express = ExpressToNetworkBox(self.app)
            # move
            async def moveTo(cert_bytes):
                cert_name = sv2.parse_certificate(cert_bytes).name
                express.put(cert_name, cert_bytes)
                return False
            local_box = Sqlite3Box(db_path)
            await local_box.search(Name.from_str("/"), moveTo)
            security_manager = EnvelopeImpl(self.app, TpmFile(tpm_path), local_box, express)
            with open(anchor_path, "r") as af:
                anchor_bytes = base64.b64decode(af.read())
            with open(model_path, "r") as lf:
                model_bytes = base64.b64decode(lf.read())
            chk.DEFAULT_USER_FNS.update(
                {'$eq_any': lambda c, args: any(x == c for x in args)}
            )
            await security_manager.set(anchor_bytes, LvsModel.parse(model_bytes), chk.DEFAULT_USER_FNS)
            return security_manager
        if self.using_envelope:
            self.envelope = await load_envelope(self.config["trust_anchor"], self.config["lvs_model"], self.config["box_sqlite3_path"], self.config["tpm"])
            self.sec_ops = SecurityOptions(SigningInfo(SignatureType.DIGEST_SHA256),
                                        ValidatingInfo(ValidatingInfo.get_validator(SignatureType.DIGEST_SHA256)),
                                        SigningInfo(SignatureType.DIGEST_SHA256), [],
                                        self.envelope)
        else:
            self.sec_ops = SecurityOptions(SigningInfo(SignatureType.DIGEST_SHA256),
                                        ValidatingInfo(ValidatingInfo.get_validator(SignatureType.DIGEST_SHA256)),
                                        SigningInfo(SignatureType.DIGEST_SHA256), [],
                                        None)
        self.svs = SVSync(self.app, Name.normalize(self.config['repo_prefix'] + "/group"), Name.normalize(self.node_name), self.svs_missing_callback, storage=self.svs_storage,
                          securityOptions=self.sec_ops)
        await aio.sleep(5)
        while True:
            await aio.sleep(self.config['loop_period'] / 1000.0)
            self.periodic()

    def periodic(self):
        self.tracker.detect()
        if self.tracker.beat():
            self.send_heartbeat()
            self.tracker.reset(self.node_name)
        self.backup_list_check()
        self.claim()
        self.check_garbage()

    def svs_missing_callback(self, missing_list):
        aio.ensure_future(self.on_missing_svs_messages(missing_list))
    async def on_missing_svs_messages(self, missing_list):
        # if missing list is greater than 100 messages, bootstrap
        for i in missing_list:
            if i.nid == self.config["node_name"]:
                self.tracker.restart(self.config["node_name"])
                # bootstrap
                continue
            while i.lowSeqno <= i.highSeqno:
                message_bytes = await self.svs.fetchData(Name.from_str(i.nid), i.lowSeqno)
                if message_bytes == None:
                    continue
                message = Message.specify(i.nid, i.lowSeqno, message_bytes)
                self.tracker.reset(i.nid)
                aio.ensure_future(message.apply(self.global_view))
                i.lowSeqno = i.lowSeqno + 1

    def send_heartbeat(self):
        heartbeat_message = HeartbeatMessageTlv()
        heartbeat_message.node_name = Name.to_bytes(self.config['node_name'])
        heartbeat_message.favor_parameters = FavorParameters()
        heartbeat_message.favor_parameters.rtt = self.config['rtt']
        heartbeat_message.favor_parameters.num_users = self.config['num_users']
        heartbeat_message.favor_parameters.bandwidth = self.config['bandwidth']
        heartbeat_message.favor_parameters.network_cost = self.config['network_cost']
        heartbeat_message.favor_parameters.storage_cost = self.config['storage_cost']
        heartbeat_message.favor_parameters.remaining_storage = self.config['remaining_storage']
        message = Message()
        message.type = MessageTypes.HEARTBEAT
        message.value = heartbeat_message.encode()
        try:
            # next_state_vector = self.svs.getCore().getStateTable().getSeqno(Name.to_str(Name.from_str(self.config['node_name']))) + 1
            next_state_vector = self.svs.getCore().getSeqno() + 1
        except TypeError:
            next_state_vector = 0
        # TODO: Calculate a node's self favor
        # favor = FavorCalculator.self_favor()
        favor = 1.00
        self.global_view.update_node(self.config['node_name'], favor, next_state_vector)
        self.svs.publishData(message.encode())

    def backup_list_check(self):
        underreplicated_files = self.global_view.get_underreplicated_files()
        for underreplicated_file in underreplicated_files:
            deficit = underreplicated_file['desired_copies'] - len(underreplicated_file['stores'])
            for backuped_by in underreplicated_file['backups']:
                if (backuped_by['node_name'] == self.config['node_name']) and (backuped_by['rank'] < deficit):
                    self.fetch_file(underreplicated_file['file_name'], underreplicated_file['packets'], underreplicated_file['packet_size'], underreplicated_file['fetch_path'])

    def claim(self):
        # TODO: possibility based on # active sessions and period
        if random.random() < 0.618:
            return
        backupable_files = self.global_view.get_backupable_files()
        for backupable_file in backupable_files:
            if random.random() < 0.618:
                continue
            # print(json.dumps(backupable_insertion['stores']))
            # print(json.dumps(backupable_insertion['backups']))
            already_in = False
            for stored_by in backupable_file['stores']:
                if stored_by == self.config['node_name']:
                    already_in = True
                    break
            for backuped_by in backupable_file['backups']:
                if backuped_by['node_name'] == self.config['node_name']:
                    already_in = True
                    break
            if already_in == True:
                continue
            if len(backupable_file['backups']) == 0 and len(backupable_file['stores']) == 0:
                continue
            authorizer = None
            if len(backupable_file['backups']) == 0:
                authorizer = {
                    'node_name': backupable_file['stores'][-1],
                    'rank': -1,
                    'nonce': backupable_file['file_name']
                }
            else:
                authorizer = backupable_file['backups'][-1]
            # generate claim (request) msg and send
            # claim tlv
            favor = 1.85
            claim_message = ClaimMessageTlv()
            claim_message.node_name = self.config['node_name'].encode()
            claim_message.favor = str(favor).encode()
            claim_message.file_name = Name.from_str(backupable_file['file_name'])
            claim_message.type = ClaimTypes.REQUEST
            claim_message.claimer_node_name = self.config['node_name'].encode()
            claim_message.claimer_nonce = secrets.token_hex(4).encode()
            claim_message.authorizer_node_name = authorizer['node_name'].encode()
            claim_message.authorizer_nonce = authorizer['nonce'].encode()
            # claim msg
            message = Message()
            message.type = MessageTypes.CLAIM
            message.value = claim_message.encode()
            self.svs.publishData(message.encode())
            self.logger.info(f"[MSG][CLAIM.R]* nam={self.config['node_name']};fil={backupable_file['file_name']}")

    def store(self, file_name: str):
        favor = 1.85
        store_message = StoreMessageTlv()
        store_message.node_name = self.config['node_name'].encode()
        store_message.favor = str(favor).encode()
        store_message.file_name = Name.from_str(file_name)
        message = Message()
        message.type = MessageTypes.STORE
        message.value = store_message.encode()

        self.global_view.store_file(file_name, self.config['node_name'])
        self.svs.publishData(message.encode())
        self.logger.info(f"[MSG][STORE]*   nam={self.config['node_name']};fil={file_name}")

    def fetch_file(self, file_name: str, packets: int, packet_size: int, fetch_path: str):
        self.file_fetcher.fetch_file_from_client(file_name, packets, packet_size, fetch_path)

    def check_garbage(self):
        """
        Checks for database garbage daily.
        """
        current_time = time.time()
        hours_since_last_collection = (current_time - self.last_garbage_collect_t) / (60*60)
        if hours_since_last_collection >= 24:
            collect_db_garbage(self.global_view, self.svs, self.config, self.logger)
            self.last_garbage_collect_t = time.time()

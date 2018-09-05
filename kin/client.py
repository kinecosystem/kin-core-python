# -*- coding: utf-8 -*

# Copyright (C) 2018 Kin Foundation
import requests

from .config import SDK_USER_AGENT
from . import errors as KinErrors
from .blockchain.keypair import Keypair
from .blockchain.builder import Builder
from .blockchain.horizon import Horizon
from .transactions import OperationTypes
from .account import KinAccount, AccountStatus
from .blockchain.horizon_models import AccountData, TransactionData
from .blockchain.utils import is_valid_address, is_valid_transaction_hash, is_valid_secret_key
from .transactions import SimplifiedTransaction
from .version import __version__

import logging
logger = logging.getLogger(__name__)


class KinClient(object):
    """
    The :class:`kin.KinClient` class is the primary interface to the KIN Python SDK based on Kin Blockchain.
    It maintains a connection context with a Horizon node and hides all the specifics of dealing with Kin REST API.
    """

    def __init__(self, environment):
        """Create a new instance of the KinClient to query the Kin blockchain.
        :param `kin.Environment` environment: an environment for the client to point to.

        :return: An instance of the KinClient.
        :rtype: :class:`KinErrors.KinClient`
        """

        self.environment = environment
        self.network = environment.name

        # init our asset
        self.kin_asset = environment.kin_asset

        self.horizon = Horizon(horizon_uri=environment.horizon_uri, user_agent=SDK_USER_AGENT)
        logger.info('Kin SDK inited on network {}, horizon endpoint {}'.format(self.network, self.horizon.horizon_uri))

    def kin_account(self, seed, channels=None, channel_secret_keys=None, create_channels=False):
        """
        Create a new instance of a KinAccount to perform authenticated operations on the blockchain.
        :param str seed: The secret seed of the account that will be used
        :param int channels: Number of channels to use
        :param list of str channel_secret_keys: A list of seeds to be used as channels
        :param boolean create_channels: Should the sdk create the channel accounts
        :return: An instance of KinAccount
        :rtype: :class:`kin.KinAccount`

        :raises: :class:`KinErrors.AccountNotFoundError`: if SDK wallet or channel account is not yet created.
        :raises: :class:`KinErrors.AccountNotActivatedError`: if SDK wallet account is not yet activated.
        """

        # Create a new kin account, using self as the KinClient to be used
        return KinAccount(seed, self, channels, channel_secret_keys, create_channels)

    def get_config(self):
        """Get system configuration data and online status.
        :return: a dictionary containing the data
        :rtype: dict
        """
        status = {
            'sdk_version': __version__,
            'environment': self.environment.name,
            'kin_asset': {
                'code': self.kin_asset.code,
                'issuer': self.kin_asset.issuer
            },
            'horizon': {
                'uri': self.horizon.horizon_uri,
                'online': False,
                'error': None,
            },
            'transport': {
                'pool_size': self.horizon.pool_size,
                'num_retries': self.horizon.num_retries,
                'request_timeout': self.horizon.request_timeout,
                'retry_statuses': self.horizon.status_forcelist,
                'backoff_factor': self.horizon.backoff_factor,
            }
        }

        # now check Horizon connection
        try:
            self.horizon.query('')
            status['horizon']['online'] = True
        except Exception as e:
            status['horizon']['error'] = str(e)

        return status

    def get_account_balances(self, address):
        """
        Get the KIN and XLM balance of a given account
        :param str address: the public address of the account to query
        :return: a dictionary containing the balances
        :rtype: dict

        :raises: ValueError: if the provided address has the wrong format.
        :raises: :class:`KinErrors.AccountNotFoundError`: if the account does not exist.
        """
        account_data = self.get_account_data(address)
        balances = {}
        for balance in account_data.balances:
            if balance.asset_code == self.kin_asset.code and \
                            balance.asset_issuer == self.kin_asset.issuer:
                balances['KIN'] = balance.balance
            elif balance.asset_type == 'native':
                balances['XLM'] = balance.balance

        return balances

    def get_account_status(self, address):
        """
        Get a given account status
        :param str address: The public address of the account to query.
        :return: One the possible account statuses.
        :rtype: :enum:`kin.AccountStatus`
        """
        try:
            balances = self.get_account_balances(address)
        except KinErrors.AccountNotFoundError:
            return AccountStatus.NOT_CREATED

        try:
            balances['KIN']
        except KeyError:
            return AccountStatus.NOT_ACTIVATED

        return AccountStatus.ACTIVATED

    def get_account_data(self, address):
        """Get account data.

        :param str address: the public address of the account to query.

        :return: account data
        :rtype: :class:`kin.AccountData`

        :raises: ValueError: if the provided address has a wrong format.
        :raises: :class:`KinErrors.AccountNotFoundError`: if the account does not exist.
        """
        # TODO: might want to simplify the returning data
        if not is_valid_address(address):
            raise ValueError('invalid address: {}'.format(address))

        try:
            acc = self.horizon.account(address)
            return AccountData(acc, strict=False)
        except Exception as e:
            err = KinErrors.translate_error(e)
            raise KinErrors.AccountNotFoundError(address) if \
                isinstance(err, KinErrors.ResourceNotFoundError) else err

    def get_transaction_data(self, tx_hash, simple=True):
        """Gets transaction data.

        :param str tx_hash: transaction hash.
        :param boolean simple: (optional) returns a simplified transaction object

        :return: transaction data
        :rtype: :class:`kin.TransactionData` or `kin.SimplifiedTransaction`

        :raises: ValueError: if the provided hash is invalid.
        :raises: :class:`KinErrors.ResourceNotFoundError`: if the transaction does not exist.
        :raises: :class:`KinErrors.CantSimplifyError`: if the tx is too complex to simplify
        """
        if not is_valid_transaction_hash(tx_hash):
            raise ValueError('invalid transaction hash: {}'.format(tx_hash))

        try:
            tx = self.horizon.transaction(tx_hash)

            # get transaction operations
            tx_ops = self.horizon.transaction_operations(tx['hash'], params={'limit': 100})
            tx['operations'] = tx_ops['_embedded']['records']

            tx_data = TransactionData(tx, strict=False)
        except Exception as e:
            raise KinErrors.translate_error(e)

        if simple:
            return SimplifiedTransaction(tx_data, self.kin_asset)
        return tx_data

    def verify_kin_payment(self, tx_hash, source, destination, amount, memo=None, check_memo=False):
        """
        Verify that a give tx matches the desired parameters
        :param str tx_hash: The hash of the transaction to query
        :param str source: The expected source account
        :param str destination: The expected destination account
        :param float amount: The expected amount
        :param str memo: (optional) The expected memo
        :param boolean check_memo: (optional) Should the memo match
        :return: True/False
        :rtype: boolean
        """

        tx = self.get_transaction_data(tx_hash)
        operation = tx.operation
        if operation.type != OperationTypes.PAYMENT:
            return False
        if operation.asset != self.kin_asset.code:
            return False
        if source != tx.source or destination != operation.destination or amount != operation.amount:
            return False
        if check_memo and memo != tx.memo:
            return False

        return True

    def friendbot(self, address):
        """
        Use the friendbot service to create and fund an account
        :param str address: The address to create and fund
        :return: the hash of the friendobt transaction
        :rtype str

        :raises ValueError: if no friendbot service was provided
        :raises ValueError: if the address is invalid
        :raises :class: `KinErrors.AccountExistsError`: if the account already exists
        :raises :class: `KinErrors.FriendbotError`: If the friendbot request failed
        """

        if self.environment.friendbot_url is None:
            raise ValueError("No friendbot service was configured for this client's environments")

        if not is_valid_address(address):
            raise ValueError('invalid address: {}'.format(address))
        if self.get_account_status(address) != AccountStatus.NOT_CREATED:
            raise KinErrors.AccountExistsError(address)

        response = requests.get(self.environment.friendbot_url, params={'addr': address})
        if response.ok:
            return response.json()['hash']
        else:
            raise KinErrors.FriendbotError(response.status_code, response.text)

    def activate_account(self, seed):
        """
        Activate an account.
        :param str seed: The secret seed of the account to activate
        :return: the hash of the transaction
        :rtype: str
        """

        # Check seed
        if not is_valid_secret_key(seed):
            raise ValueError('invalid seed {}'.format(seed))
        # Check the account status
        address = Keypair.address_from_seed(seed)
        status = self.get_account_status(address)

        if status == AccountStatus.NOT_CREATED:
            raise KinErrors.AccountNotFoundError(address)
        if status == AccountStatus.ACTIVATED:
            raise KinErrors.AccountActivatedError(address)

        builder = Builder(self.environment.name, self.horizon, seed)
        builder.append_trust_op(self.kin_asset.issuer, self.kin_asset.code)
        builder.sign()
        reply = builder.submit()

        return reply['hash']

    def monitor_accounts_payments(self, addresses, callback_fn):
        """Monitor KIN payment transactions related to the accounts identified by provided addresses.
        NOTE: the function starts a background thread.

        :param list of str addresses: the addresses of the accounts to query.

        :param callback_fn: the function to call on each received payment as `callback_fn(address, tx_data)`.
        :type: callable[[str, :class:`kin.TransactionData`], None]

        :return: an event to stop the monitoring
        :rtype: threading.Event

        :raises: ValueError: when no addresses are given.
        :raises: ValueError: if one of the provided addresses has a wrong format.
        :raises: :class:`KinErrors.AccountNotFoundError`: if one of the provided accounts is not yet created.
        """
        if not addresses:
            raise ValueError('no addresses to monitor')

        for address in addresses:
            if not is_valid_address(address):
                raise ValueError('invalid address: {}'.format(address))

        for address in addresses:
            if self.get_account_status(address) == AccountStatus.NOT_CREATED:
                raise KinErrors.AccountNotFoundError(addresses)

        # Currently, due to nonstandard SSE implementation in Horizon, using cursor=now will hang.
        # Instead, we determine the cursor ourselves.
        # Fix will be for horizon to send any message just to start a connection
        params = {}
        if len(addresses) == 1:
            reply = self.horizon.account_transactions(addresses[0], params={'order': 'desc', 'limit': 2})
        else:
            reply = self.horizon.transactions(params={'order': 'desc', 'limit': 2})

        if len(reply['_embedded']['records']) == 2:
            cursor = TransactionData(reply['_embedded']['records'][1], strict=False).paging_token
            params = {'cursor': cursor}

        # make synchronous SSE request (will raise errors in the current thread)
        if len(addresses) == 1:
            events = self.horizon.account_transactions(addresses[0], sse=True, params=params)
        else:
            events = self.horizon.transactions(sse=True, params=params)

        # asynchronous event processor
        def event_processor(stop_event):
            import json
            for event in events:
                if stop_event.is_set():
                    return
                if event.event != 'message':
                    continue
                try:
                    tx = json.loads(event.data)

                    # get transaction operations
                    tx_ops = self.horizon.transaction_operations(tx['hash'], params={'limit': 100})
                    tx['operations'] = tx_ops['_embedded']['records']

                    # deserialize
                    tx_data = TransactionData(tx, strict=False)

                    # iterate over transaction operations and see if there's a match
                    for op_data in tx_data.operations:
                        if op_data.asset_type == 'native':
                            continue
                        if op_data.asset_code != self.kin_asset.code or op_data.asset_issuer \
                                != self.kin_asset.issuer:
                            continue

                        try:
                            tx_data = SimplifiedTransaction(tx_data,self.kin_asset)
                        except KinErrors.CantSimplifyError:
                            break
                        if tx_data.operation.type is not OperationTypes.PAYMENT:
                            break

                        if len(addresses) == 1:
                            callback_fn(addresses[0], tx_data)
                            break
                        elif op_data.from_address in addresses:
                            callback_fn(op_data.from_address, tx_data)
                            break
                        elif op_data.to_address in addresses:
                            callback_fn(op_data.to_address, tx_data)
                            break

                except Exception as ex:
                    logger.exception(ex)
                    continue

        # start monitoring thread
        import threading
        stop_event = threading.Event()
        t = threading.Thread(target=event_processor,args=(stop_event,))
        t.daemon = True
        t.start()
        return stop_event
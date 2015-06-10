"""
Copyright 2015 Zalando SE

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance with the
License. You may obtain a copy of the License at

http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the specific
 language governing permissions and limitations under the License.
"""

import logging
from lizzy.senza_wrapper import Senza, ExecutionError
from lizzy.models.stack import Stack


_failed_to_get_domains = object()  # sentinel value for when we failed to get domains from senza


class Deployer():

    logger = logging.getLogger('lizzy.job.deployer')

    def __init__(self, region: str, lizzy_stacks: dict, cf_stacks: dict, stack: Stack):
        self.senza = Senza(region)
        self.lizzy_stacks = lizzy_stacks  # All stacks in lizzy
        self.cf_stacks = cf_stacks  # Stacks on CloudFormation
        self.stack = stack  # Stack to deploy

    @property
    def log_info(self) -> dict:
        log_info = dict()
        log_info['lizzy.stack.name'] = self.stack.stack_name
        log_info['lizzy.stack.id'] = self.stack.stack_id
        log_info['lizzy.stack.traffic'] = self.stack.traffic

        cloud_formation_status = self._get_stack_status()
        if cloud_formation_status:
            log_info['cf_status'] = cloud_formation_status

        return log_info

    def _get_stack_status(self) -> str:
        """
        Get Stack Status in CloudFormation
        """
        try:
            cf_status = self.cf_stacks[self.stack.stack_name][self.stack.stack_version]['status']
        except KeyError:
            cf_status = None
        return cf_status

    def default(self) -> str:
        """
        Handler for all other deployment status

        It replaces the status with the one from Cloud Formation or marks the deployment as removed if it no
        longer exists.
        """

        # TODO update traffic

        self.logger.debug("Updating stack status based on AWS CF.", extra=self.log_info)
        cloud_formation_status = self._get_stack_status()
        if cloud_formation_status is not None:
            self.logger.debug("Stack status updated", extra=self.log_info)
            new_status = 'CF:{}'.format(cloud_formation_status)
        else:
            # If this happens is because the stack was removed from aws
            self.logger.info("Stack no longer exists, marking as removed", extra=self.log_info)
            new_status = 'LIZZY:REMOVED'
        return new_status

    def deploying(self) -> str:
        """
        Handler for when deployment status=='LIZZY:DEPLOYING'

        Checks if the stack status changed.
        """
        cloud_formation_status = self._get_stack_status()

        if cloud_formation_status is None:  # Stack no longer exist.
            self.logger.info("Stack no longer exists, marking as removed.", extra=self.log_info)
            new_status = 'LIZZY:REMOVED'
        elif cloud_formation_status == 'CREATE_IN_PROGRESS':
            self.logger.debug("Stack is still deploying.", extra=self.log_info)
            new_status = 'LIZZY:DEPLOYING'
        elif cloud_formation_status == 'CREATE_COMPLETE':
            self.logger.info("Stack created.", extra=self.log_info)
            new_status = 'LIZZY:DEPLOYED'
        else:
            self.logger.info("Updating stack status based on AWS CF.", extra=self.log_info)
            new_status = 'CF:{}'.format(cloud_formation_status)

        return new_status

    def deployed(self):
        """
        Handler for when deployment status=='LIZZY:DEPLOYED'

        Removes old versions and switches the traffic.
        """

        cloud_formation_status = self._get_stack_status()
        if cloud_formation_status is None:  # Stack no longer exist.
            self.logger.info("Stack no longer exists, marking as removed", extra=self.log_info)
            return 'LIZZY:REMOVED'

        all_versions = sorted(self.lizzy_stacks[self.stack.stack_name].keys())
        self.logger.debug("Existing versions: %s", all_versions, extra=self.log_info)
        # we want to keep only two versions
        number_of_versions_to_keep = self.stack.keep_stacks + 1  # keep provided old stacks + 1
        versions_to_remove = all_versions[:-number_of_versions_to_keep]
        self.logger.debug("Versions to be removed: %s", versions_to_remove, extra=self.log_info)
        for version in versions_to_remove:
            stack_id = '{}-{}'.format(self.stack.stack_name, version)
            log_info = {'lizzy.stack.id': stack_id, 'lizzy.new_stack.id': self.stack.stack_id}
            self.logger.info("Removing stack...", extra=log_info)
            try:
                self.senza.remove(self.stack.stack_name, version)
                self.logger.info("Stack removed.", extra=log_info)
            except Exception:
                self.logger.exception("Failed to remove stack.", extra=log_info)

        # Switch all traffic to new version
        try:
            domains = self.senza.domains(self.stack.stack_name)
        except ExecutionError:
            self.logger.exception("Failed to get domains. Traffic will no be switched.", extra=self.log_info)
            domains = _failed_to_get_domains

        if not domains:
            self.logger.info("App doesn't have a domain so traffic will not be switched.", extra=self.log_info)
        elif domains is not _failed_to_get_domains:
            self.logger.info("Switching app traffic.",  extra=self.log_info)
            try:
                self.senza.traffic(stack_name=self.stack.stack_name,
                                   stack_version=self.stack.stack_version,
                                   percentage=self.stack.traffic)
            except ExecutionError:
                self.logger.exception("Failed to switch app traffic.",  extra=self.log_info)

        return 'CF:{}'.format(cloud_formation_status)

    def change(self) -> str:
        """
        Update stack. Currently this only changes the traffic.

        Returns the cloud formation status
        """

        try:
            domains = self.senza.domains(self.stack.stack_name)
        except ExecutionError:
            self.logger.exception("Failed to get domains. Traffic will no be switched.", extra=self.log_info)
            domains = _failed_to_get_domains

        if not domains:
            self.logger.info("App doesn't have a domain so traffic will not be switched.", extra=self.log_info)
        elif domains is not _failed_to_get_domains:
            self.logger.info("Switching app traffic to stack.",  extra=self.log_info)
            try:
                self.senza.traffic(stack_name=self.stack.stack_name,
                                   stack_version=self.stack.stack_version,
                                   percentage=self.stack.traffic)
            except ExecutionError:
                self.logger.exception("Failed to switch app traffic.",  extra=self.log_info)

        return self.default()

    def handle(self) -> str:
        """
        Does the right step for deployment status
        """
        if self.stack.status == 'LIZZY:NEW':
            return self.new()
        elif self.stack.status == 'LIZZY:DEPLOYING':
            return self.deploying()
        elif self.stack.status == 'LIZZY:DEPLOYED':
            return self.deployed()
        elif self.stack.status == 'LIZZY:ERROR':
            return 'LIZZY:ERROR'  # This is hardcoded because there is nothing more that can be done about it
        elif self.stack.status == 'LIZZY:CHANGE':
            return self.change()
        else:
            return self.default()

    def new(self) -> str:
        """
        Handler for when deployment status=='LIZZY:NEW'
        By default the stack is created
        """
        self.logger.info("Creating stack...", extra=self.log_info)
        if self.senza.create(self.stack.senza_yaml, self.stack.stack_version, self.stack.image_version):
            self.logger.info("Stack created.",  extra=self.log_info)
            new_status = 'LIZZY:DEPLOYING'
        else:
            self.logger.error("Error creating stack.",  extra=self.log_info)
            new_status = 'LIZZY:ERROR'

        return new_status
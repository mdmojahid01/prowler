from unittest import mock

from tests.providers.azure.azure_fixtures import (
    AZURE_SUBSCRIPTION_ID,
    set_mocked_azure_provider,
)


class Test_monitor_storage_account_with_activity_logs_is_private:
    def test_monitor_storage_account_with_activity_logs_is_private_no_subscriptions(
        self,
    ):
        monitor_client = mock.MagicMock
        monitor_client.diagnostics_settings = {}

        with (
            mock.patch(
                "prowler.providers.common.provider.Provider.get_global_provider",
                return_value=set_mocked_azure_provider(),
            ),
            mock.patch(
                "prowler.providers.azure.services.monitor.monitor_storage_account_with_activity_logs_is_private.monitor_storage_account_with_activity_logs_is_private.monitor_client",
                new=monitor_client,
            ),
        ):
            from prowler.providers.azure.services.monitor.monitor_storage_account_with_activity_logs_is_private.monitor_storage_account_with_activity_logs_is_private import (
                monitor_storage_account_with_activity_logs_is_private,
            )

            check = monitor_storage_account_with_activity_logs_is_private()
            result = check.execute()
            assert len(result) == 0

    def test_no_diagnostic_settings(self):
        monitor_client = mock.MagicMock
        monitor_client.diagnostics_settings = {AZURE_SUBSCRIPTION_ID: []}
        with (
            mock.patch(
                "prowler.providers.common.provider.Provider.get_global_provider",
                return_value=set_mocked_azure_provider(),
            ),
            mock.patch(
                "prowler.providers.azure.services.monitor.monitor_storage_account_with_activity_logs_is_private.monitor_storage_account_with_activity_logs_is_private.monitor_client",
                new=monitor_client,
            ),
        ):
            from prowler.providers.azure.services.monitor.monitor_storage_account_with_activity_logs_is_private.monitor_storage_account_with_activity_logs_is_private import (
                monitor_storage_account_with_activity_logs_is_private,
            )

            check = monitor_storage_account_with_activity_logs_is_private()
            result = check.execute()
            assert len(result) == 0

    def test_diagnostic_settings_configured(self):
        monitor_client = mock.MagicMock
        storage_client = mock.MagicMock

        with (
            mock.patch(
                "prowler.providers.common.provider.Provider.get_global_provider",
                return_value=set_mocked_azure_provider(),
            ),
            mock.patch(
                "prowler.providers.azure.services.monitor.monitor_storage_account_with_activity_logs_is_private.monitor_storage_account_with_activity_logs_is_private.monitor_client",
                new=monitor_client,
            ),
        ):
            with mock.patch(
                "prowler.providers.azure.services.monitor.monitor_storage_account_with_activity_logs_is_private.monitor_storage_account_with_activity_logs_is_private.storage_client",
                new=storage_client,
            ):
                from prowler.providers.azure.services.monitor.monitor_service import (
                    DiagnosticSetting,
                )
                from prowler.providers.azure.services.monitor.monitor_storage_account_with_activity_logs_is_private.monitor_storage_account_with_activity_logs_is_private import (
                    monitor_storage_account_with_activity_logs_is_private,
                )
                from prowler.providers.azure.services.storage.storage_service import (
                    Account,
                    BlobProperties,
                    DeleteRetentionPolicy,
                    NetworkRuleSet,
                )

                monitor_client.diagnostics_settings = {
                    AZURE_SUBSCRIPTION_ID: [
                        DiagnosticSetting(
                            id="id",
                            logs=[
                                mock.MagicMock(category="Administrative", enabled=True),
                                mock.MagicMock(category="Security", enabled=True),
                                mock.MagicMock(category="ServiceHealth", enabled=False),
                                mock.MagicMock(category="Alert", enabled=True),
                                mock.MagicMock(
                                    category="Recommendation", enabled=False
                                ),
                                mock.MagicMock(category="Policy", enabled=True),
                                mock.MagicMock(category="Autoscale", enabled=False),
                            ],
                            storage_account_id="/subscriptions/1234a5-123a-123a-123a-1234567890ab/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/storageaccountname1",
                            storage_account_name="storageaccountname1",
                            name="name",
                        ),
                        DiagnosticSetting(
                            id="id2",
                            logs=[
                                mock.MagicMock(category="Administrative", enabled=True),
                                mock.MagicMock(category="Security", enabled=True),
                                mock.MagicMock(category="ServiceHealth", enabled=False),
                                mock.MagicMock(category="Alert", enabled=True),
                                mock.MagicMock(
                                    category="Recommendation", enabled=False
                                ),
                                mock.MagicMock(category="Policy", enabled=True),
                                mock.MagicMock(category="Autoscale", enabled=False),
                            ],
                            storage_account_id="/subscriptions/1224a5-123a-123a-123a-1234567890ab/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/storageaccountname2",
                            storage_account_name="storageaccountname2",
                            name="name2",
                        ),
                    ]
                }
                storage_client.storage_accounts = {
                    AZURE_SUBSCRIPTION_ID: [
                        Account(
                            id="/subscriptions/1234a5-123a-123a-123a-1234567890ab/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/storageaccountname1",
                            name="storageaccountname1",
                            resouce_group_name="rg",
                            enable_https_traffic_only=True,
                            infrastructure_encryption=True,
                            allow_blob_public_access=True,
                            network_rule_set=NetworkRuleSet(
                                bypass="AzureServices", default_action="Allow"
                            ),
                            encryption_type="Microsoft.Storage",
                            minimum_tls_version="TLS1_2",
                            private_endpoint_connections=[],
                            key_expiration_period_in_days=365,
                            location="euwest",
                            blob_properties=BlobProperties(
                                id="id",
                                name="name",
                                type="type",
                                default_service_version="default_service_version",
                                container_delete_retention_policy=DeleteRetentionPolicy(
                                    enabled=True, days=7
                                ),
                                versioning_enabled=True,
                            ),
                        ),
                        Account(
                            id="/subscriptions/1224a5-123a-123a-123a-1234567890ab/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/storageaccountname2",
                            name="storageaccountname2",
                            resouce_group_name="rg",
                            enable_https_traffic_only=False,
                            infrastructure_encryption=True,
                            allow_blob_public_access=False,
                            network_rule_set=NetworkRuleSet(
                                bypass="AzureServices", default_action="Allow"
                            ),
                            encryption_type="Microsoft.Storage",
                            minimum_tls_version="TLS1_2",
                            private_endpoint_connections=[],
                            key_expiration_period_in_days=365,
                            location="euwest",
                            blob_properties=BlobProperties(
                                id="id",
                                name="name",
                                type="type",
                                default_service_version="default_service_version",
                                container_delete_retention_policy=DeleteRetentionPolicy(
                                    enabled=True, days=7
                                ),
                                versioning_enabled=False,
                            ),
                        ),
                    ]
                }
                check = monitor_storage_account_with_activity_logs_is_private()
                result = check.execute()
                assert len(result) == 2
                assert result[0].subscription == AZURE_SUBSCRIPTION_ID
                assert result[0].status == "FAIL"
                assert result[0].location == "euwest"
                assert (
                    result[0].resource_id
                    == "/subscriptions/1234a5-123a-123a-123a-1234567890ab/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/storageaccountname1"
                )
                assert result[0].resource_name == "storageaccountname1"
                assert (
                    result[0].status_extended
                    == f"Blob public access enabled in storage account {storage_client.storage_accounts[AZURE_SUBSCRIPTION_ID][0].name} storing activity logs in subscription {AZURE_SUBSCRIPTION_ID}."
                )
                assert result[1].subscription == AZURE_SUBSCRIPTION_ID
                assert result[1].status == "PASS"
                assert result[1].location == "euwest"
                assert (
                    result[1].resource_id
                    == "/subscriptions/1224a5-123a-123a-123a-1234567890ab/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/storageaccountname2"
                )
                assert result[1].resource_name == "storageaccountname2"
                assert (
                    result[1].status_extended
                    == f"Blob public access disabled in storage account {storage_client.storage_accounts[AZURE_SUBSCRIPTION_ID][1].name} storing activity logs in subscription {AZURE_SUBSCRIPTION_ID}."
                )

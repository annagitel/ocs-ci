"""
PV Create/Delete & Memory Leak Test: Test the PVC limit with 3 worker nodes
with & without any IO create and delete the PVCs and check for memory leak
TO DO: This Test needs to be executed in Scaled setup,
Adding node scale is yet to be supported.
"""
import logging
import pytest
import random
import time

from concurrent.futures import ThreadPoolExecutor
from tests import helpers
from ocs_ci.ocs import constants
from ocs_ci.ocs.resources import pod, pvc
from ocs_ci.framework.testlib import scale, E2ETest

log = logging.getLogger(__name__)


class BasePvcPodCreateDelete(E2ETest):
    """
    Base Class to create/delete PVC and POD
    """

    def create_pvc_pod(self, rbd_sc_obj, cephfs_sc_obj, number_of_pvc, size, start_io):
        """
        Function to create multiple PVC of different type and created pods on them.

        Args:
            rbd_sc_obj (obj_dict): rbd storageclass object
            cephfs_sc_obj (obj_dict): cephfs storageclass object
            number_of_pvc (int): pvc count to be created for each types
            size (str): size of each pvc to be created eg: '10Gi'
            start_io (boolean): Ture to start and False not to start IO
        """
        log.info(f"Create {number_of_pvc} pvcs and pods")
        self.delete_pod_count = round(number_of_pvc / 2)
        cephfs_pvcs = helpers.create_multiple_pvc_parallel(
            cephfs_sc_obj, self.namespace, number_of_pvc, size,
            access_modes=[constants.ACCESS_MODE_RWO, constants.ACCESS_MODE_RWX]
        )
        rbd_pvcs = helpers.create_multiple_pvc_parallel(
            rbd_sc_obj, self.namespace, number_of_pvc, size,
            access_modes=[constants.ACCESS_MODE_RWO, constants.ACCESS_MODE_RWX]
        )
        # Appending all the pvc obj to base case param for cleanup and evaluation
        self.all_pvc_obj.extend(cephfs_pvcs + rbd_pvcs)
        # Create pods with above pvc list
        cephfs_pods = helpers.create_pods_parallel(
            cephfs_pvcs, self.namespace, constants.CEPHFS_INTERFACE
        )
        rbd_rwo_pods = list()
        # TODO: RBD RWX pod creation
        for pvc_obj in rbd_pvcs:
            if not pvc.get_pvc_access_mode(pvc_obj) == constants.ACCESS_MODE_RWX:
                rbd_rwo_pods.append(pvc_obj)
        rbd_pods = helpers.create_pods_parallel(
            rbd_rwo_pods, self.namespace, constants.CEPHBLOCKPOOL
        )
        temp_pod_objs = list()
        temp_pod_objs.extend(cephfs_pods + rbd_pods)
        # Appending all the pod obj to base case param for cleanup and evaluation
        self.all_pod_obj.extend(temp_pod_objs)

        # IO will start based on TC requirement
        if start_io:
            with ThreadPoolExecutor() as executor:
                for pod_obj in temp_pod_objs:
                    executor.submit(pod_obj.run_io('fs', size='512M'))

    def delete_pvc_pod(self):
        """
        Function to delete pvc and pod based on the delete pod count.
        """
        log.info(f"Delete {self.delete_pod_count} pods and respective pvcs")
        temp_pod_list = random.choices(self.all_pod_obj, k=self.delete_pod_count)
        temp_pvc_list = []
        for pod_obj in temp_pod_list:
            for pvc_obj in self.all_pvc_obj:
                if pod.get_pvc_name(pod_obj) == pvc_obj.name:
                    temp_pvc_list.append(pvc_obj)
                    log.info(f"Deleting pvc {pvc_obj.name}")
                    self.all_pvc_obj.remove(pvc_obj)
            log.info(f"Deleting pod {pod_obj.name}")
            if pod_obj in self.all_pod_obj:
                self.all_pod_obj.remove(pod_obj)
        helpers.delete_objs_parallel(temp_pod_list)
        helpers.delete_objs_parallel(temp_pvc_list)

    def cleanup(self):
        """
        Function to cleanup the SC, PVC and POD objects parallel.
        """
        helpers.delete_objs_parallel(pod.get_all_pods(namespace=self.namespace))
        helpers.delete_objs_parallel(self.all_pvc_obj)
        self.rbd_sc_obj.delete()
        self.cephfs_sc_obj.delete()


@scale
@pytest.mark.parametrize(
    argnames="start_io",
    argvalues=[
        pytest.param(
            *[False], marks=pytest.mark.polarion_id("OCS-682")
        ),
        pytest.param(
            *[True], marks=pytest.mark.polarion_id("OCS-679")
        )
    ]
)
class TestPVSTOcsCreateDeletePVCsWithAndWithoutIO(BasePvcPodCreateDelete):
    """
    Class for TC OCS-682 & OCS-679 Create & Delete Cluster with 1000 PVC with
    and without IO, then Increase the PVC count to 1500. Check for Memory leak
    """
    @pytest.fixture()
    def setup_fixture(self, request):
        def finalizer():
            self.cleanup()

        request.addfinalizer(finalizer)

    @pytest.fixture()
    def namespace(self, project_factory):
        """
        Create a project for the test
        """
        proj_obj = project_factory()
        self.namespace = proj_obj.namespace

    @pytest.fixture()
    def storageclass(self, storageclass_factory):
        """
        Create Storage class for rbd and cephfs
        """
        self.rbd_sc_obj = storageclass_factory(interface=constants.CEPHBLOCKPOOL)
        self.cephfs_sc_obj = storageclass_factory(interface=constants.CEPHFILESYSTEM)

    def test_pv_scale_out_create_delete_pvcs_with_and_without_io(
        self, memory_leak_function, namespace, storageclass, setup_fixture, start_io
    ):
        self.pvc_count_1st_itr = 10
        self.pvc_count_next_itr = 10
        self.scale_pod_count = 120
        self.size = '10Gi'
        test_run_time = 300
        self.all_pvc_obj, self.all_pod_obj = ([] for i in range(2))
        self.delete_pod_count = 0

        # Identify median memory value for each worker node
        median_dict = helpers.get_memory_leak_median_value()
        log.info(f"Median dict values for memory leak {median_dict}")

        # First Iteration call to create PVC and POD
        self.create_pvc_pod(self.rbd_sc_obj, self.cephfs_sc_obj, self.pvc_count_1st_itr,
                            self.size, start_io)

        # Continue to iterate till the scale pvc limit is reached
        # Also continue to perform create and delete of pod, pvc in parallel
        while True:
            if self.scale_pod_count <= len(self.all_pod_obj):
                log.info(f"Created {self.scale_pod_count} pvc and pods")
                break
            else:
                with ThreadPoolExecutor() as executor:
                    log.info(f"Create {self.pvc_count_next_itr} and "
                             f"in parallel delete {self.delete_pod_count} "
                             f"pods & pvc")
                    thread_list = [self.delete_pvc_pod(), self.create_pvc_pod(
                        self.rbd_sc_obj, self.cephfs_sc_obj, self.pvc_count_next_itr,
                        self.size, start_io)]
                    for thread in thread_list:
                        executor.submit(thread)

        # Added sleep for test case run time and for capturing memory leak after scale
        time.sleep(test_run_time)
        helpers.memory_leak_analysis(median_dict)

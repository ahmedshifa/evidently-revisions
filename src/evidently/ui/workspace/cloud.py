from io import BytesIO
from typing import BinaryIO
from typing import Dict
from typing import List
from typing import Literal
from typing import NamedTuple
from typing import Optional
from typing import Type
from typing import Union
from typing import overload
from uuid import UUID

import pandas as pd
from requests import HTTPError
from requests import Response

from evidently.ui.api.models import OrgModel
from evidently.ui.api.models import TeamModel
from evidently.ui.base import Org
from evidently.ui.base import ProjectManager
from evidently.ui.base import Team
from evidently.ui.storage.common import NoopAuthManager
from evidently.ui.type_aliases import STR_UUID
from evidently.ui.type_aliases import ZERO_UUID
from evidently.ui.type_aliases import DatasetID
from evidently.ui.type_aliases import OrgID
from evidently.ui.type_aliases import TeamID
from evidently.ui.workspace.remote import NoopBlobStorage
from evidently.ui.workspace.remote import NoopDataStorage
from evidently.ui.workspace.remote import RemoteMetadataStorage
from evidently.ui.workspace.remote import T
from evidently.ui.workspace.view import WorkspaceView

TOKEN_HEADER_NAME = "X-Evidently-Token"


class Cookie(NamedTuple):
    key: str
    description: str
    httponly: bool


ACCESS_TOKEN_COOKIE = Cookie(
    key="app.at",
    description="",
    httponly=True,
)


class CloudMetadataStorage(RemoteMetadataStorage):
    def __init__(self, base_url: str, token: str, token_cookie_name: str):
        self.token = token
        self.token_cookie_name = token_cookie_name
        self._jwt_token: Optional[str] = None
        self._logged_in: bool = False
        super().__init__(base_url=base_url)

    def _get_jwt_token(self):
        return super()._request("/api/users/login", "GET", headers={TOKEN_HEADER_NAME: self.token}).text

    @property
    def jwt_token(self):
        if self._jwt_token is None:
            self._jwt_token = self._get_jwt_token()

        return self._jwt_token

    def _prepare_request(
        self,
        path: str,
        method: str,
        query_params: Optional[dict] = None,
        body: Optional[dict] = None,
        cookies=None,
        headers: Dict[str, str] = None,
        form_data: bool = False,
    ):
        r = super()._prepare_request(
            path=path,
            method=method,
            query_params=query_params,
            body=body,
            cookies=cookies,
            headers=headers,
            form_data=form_data,
        )
        if path == "/api/users/login":
            return r
        r.cookies[self.token_cookie_name] = self.jwt_token
        return r

    @overload
    def _request(
        self,
        path: str,
        method: str,
        query_params: Optional[dict] = None,
        body: Optional[dict] = None,
        response_model: Type[T] = ...,
        cookies=None,
        headers: Dict[str, str] = None,
        form_data: bool = False,
    ) -> T:
        pass

    @overload
    def _request(
        self,
        path: str,
        method: str,
        query_params: Optional[dict] = None,
        body: Optional[dict] = None,
        response_model: Literal[None] = None,
        cookies=None,
        headers: Dict[str, str] = None,
        form_data: bool = False,
    ) -> Response:
        pass

    def _request(
        self,
        path: str,
        method: str,
        query_params: Optional[dict] = None,
        body: Optional[dict] = None,
        response_model: Optional[Type[T]] = None,
        cookies=None,
        headers: Dict[str, str] = None,
        form_data: bool = False,
    ) -> Union[Response, T]:
        try:
            res = super()._request(
                path=path,
                method=method,
                query_params=query_params,
                body=body,
                response_model=response_model,
                cookies=cookies,
                headers=headers,
                form_data=form_data,
            )
            self._logged_in = True
            return res
        except HTTPError as e:
            if self._logged_in and e.response.status_code == 401:
                # renew token and retry
                self._jwt_token = self._get_jwt_token()
                cookies[self.token_cookie_name] = self.jwt_token
                return super()._request(
                    path,
                    method,
                    query_params,
                    body,
                    response_model,
                    cookies=cookies,
                    headers=headers,
                    form_data=form_data,
                )
            raise

    def create_org(self, org: Org) -> OrgModel:
        return self._request("/api/orgs", "POST", body=org.dict(), response_model=OrgModel)

    def list_orgs(self) -> List[OrgModel]:
        return self._request("/api/orgs", "GET", response_model=List[OrgModel])

    def create_team(self, team: Team, org_id: OrgID = None) -> TeamModel:
        return self._request(
            "/api/teams",
            "POST",
            query_params={"name": team.name, "org_id": org_id},
            response_model=TeamModel,
        )

    def add_dataset(
        self, file: BinaryIO, name: str, org_id: OrgID, team_id: TeamID, description: Optional[str]
    ) -> DatasetID:
        response: Response = self._request(
            "/api/datasets/",
            "POST",
            body={"name": name, "description": description, "file": file},
            query_params={"org_id": org_id, "team_id": team_id},
            form_data=True,
        )
        return DatasetID(response.json()["dataset_id"])

    def load_dataset(self, dataset_id: DatasetID) -> pd.DataFrame:
        response: Response = self._request(f"/api/datasets/{dataset_id}/download", "GET")
        return pd.read_parquet(BytesIO(response.content))


class NamedBytesIO(BytesIO):
    def __init__(self, initial_bytes: bytes, name: str):
        super().__init__(initial_bytes=initial_bytes)
        self.name = name


class CloudWorkspace(WorkspaceView):
    token: str
    URL: str = "https://app.evidently.cloud"

    def __init__(
        self,
        token: str,
        url: str = None,
    ):
        self.token = token
        self.url = url if url is not None else self.URL

        # todo: default org if user have only one
        user_id = ZERO_UUID  # todo: get from /me
        meta = CloudMetadataStorage(
            base_url=self.url,
            token=self.token,
            token_cookie_name=ACCESS_TOKEN_COOKIE.key,
        )

        pm = ProjectManager(
            metadata=meta,
            blob=(NoopBlobStorage()),
            data=(NoopDataStorage()),
            auth=(CloudAuthManager()),
        )
        super().__init__(
            user_id,
            pm,
        )

    def create_org(self, name: str) -> Org:
        assert isinstance(self.project_manager.metadata, CloudMetadataStorage)
        return self.project_manager.metadata.create_org(Org(name=name)).to_org()

    def list_orgs(self) -> List[Org]:
        assert isinstance(self.project_manager.metadata, CloudMetadataStorage)
        return [o.to_org() for o in self.project_manager.metadata.list_orgs()]

    def create_team(self, name: str, org_id: OrgID) -> Team:
        assert isinstance(self.project_manager.metadata, CloudMetadataStorage)
        return self.project_manager.metadata.create_team(Team(name=name), org_id).to_team()

    def add_dataset(
        self,
        data_or_path: Union[str, pd.DataFrame],
        name: str,
        org_id: STR_UUID,
        team_id: STR_UUID,
        description: Optional[str] = None,
    ) -> DatasetID:
        file: Union[NamedBytesIO, BinaryIO]
        assert isinstance(self.project_manager.metadata, CloudMetadataStorage)
        if isinstance(org_id, str):
            org_id = UUID(org_id)
        if isinstance(team_id, str):
            team_id = UUID(team_id)
        if isinstance(data_or_path, str):
            file = open(data_or_path, "rb")
        else:
            file = NamedBytesIO(b"", "data.parquet")
            data_or_path.to_parquet(file)
            file.seek(0)
        try:
            return self.project_manager.metadata.add_dataset(file, name, org_id, team_id, description)
        finally:
            file.close()

    def load_dataset(self, dataset_id: DatasetID) -> pd.DataFrame:
        assert isinstance(self.project_manager.metadata, CloudMetadataStorage)
        return self.project_manager.metadata.load_dataset(dataset_id)


class CloudAuthManager(NoopAuthManager):
    def get_team(self, team_id: TeamID) -> Optional[Team]:
        return Team(id=team_id, name="")

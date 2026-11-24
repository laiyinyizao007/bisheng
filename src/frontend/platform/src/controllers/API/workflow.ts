// @ts-strict-ignore
import { WorkFlow } from "@/types/flow";
import i18next from "i18next";
import axios from "../request";

const REQUEST_FAILED = "Request failed. Please try again.";

/**
 * Workflow node templates. The backend owns them (F005); the editor keeps no copy,
 * so a failed or malformed reply rejects and the caller reports it.
 */
export const getWorkflowNodeTemplate = async (): Promise<any[]> => {
    const templates = await axios.get("/api/v1/workflow/node_templates");
    if (!isNodeTemplateList(templates)) {
        // The reason is shown in the node panel, so it is the localized generic failure
        // (same default as the shared request layer, in case the bundle is not loaded).
        const message = i18next.t("api_errors:fallback", { defaultValue: REQUEST_FAILED });
        throw new Error(typeof message === "string" && message ? message : REQUEST_FAILED);
    }
    return templates;
}

// Every entry must be buildable into a node: a string type and a group_params list. A
// malformed entry would otherwise throw later, inside the canvas drop handler.
function isNodeTemplateList(value: unknown): value is Array<Record<string, unknown>> {
    return Array.isArray(value) && value.length > 0 && value.every(item =>
        item !== null && typeof item === "object" && typeof item.type === "string" && item.type !== ""
        && Array.isArray(item.group_params))
}

/**
 * 获取某工作流报告模板信息
 */
export const getWorkflowReportTemplate = async (key: string, flowId: string): Promise<any> => {
    return await axios.get(`/api/v1/workflow/report/file?version_key=${key}&workflow_id=${flowId}`);
}

/**
 * 创建工作流
 */
export const createWorkflowApi = async (name, desc, url, flow): Promise<any> => {
    if (url) {
        // logo保存相对路径
        url = url.replace('/bisheng', '')
    }
    const data = flow || {}
    return await axios.post("/api/v1/workflow/create", {
        ...data,
        name,
        description: desc,
        logo: url
    });
}

/**
 * 保存工作流
 */
export const saveWorkflow = async (versionId: number, data: WorkFlow): Promise<any> => {
    if (data.logo) {
        // logo保存相对路径
        data.logo = data.logo.replace(/^\/\w+/, '')
    }
    return await axios.put(`/api/v1/workflow/versions/${versionId}`, data);
}



/**
 * 删除版本.
 *
 * @param {string} versionId - 要删除的版本ID.
 * @returns {Promise<any>}.
 * @throws .
 */
export async function deleteVersion(versionId: string) {
    return await axios.delete(`/api/v1/workflow/versions/${versionId}`);
}

/**
 * 创建新的工作流版本.
 *
 * @param {object} versionData - 新版本的数据.
 * @returns {Promise<any>}.
 * @throws .
 */
export async function createWorkFlowVersion(flow_id, versionData: { name: string, description: string, original_version_id: number, data: any }) {
    return await axios.post(`/api/v1/workflow/versions?flow_id=${flow_id}`, versionData);
}

/**
 * 获取单个版本的信息.
 *
 * @param {string} versionId - 版本的ID.
 * @returns {Promise<any>}.
 * @throws .
 */
export async function getVersionDetails(versionId: string) {
    return await axios.get(`/api/v1/workflow/versions/${versionId}`);
}

/**
 * 更新版本信息.
 *
 * @param {string} versionId - 要更新的版本ID.
 * @param {object} versionData - 更新的版本数据.
 * @returns {Promise<any>}.
 * @throws .
 */
export async function updateVersion(versionId: string, versionData: { name: string, description: string, data: any }) {
    return await axios.put(`/api/v1/workflow/versions/${versionId}`, versionData);
}

/**
 * 获取工作流对应的版本列表.
 *
 * @returns {Promise<any>}.
 * @throws .
 */
export async function getWorkFlowVersions(flow_id): Promise<{ data: any[], total: number }> {
    return await axios.get(`/api/v1/workflow/versions`, {
        params: { flow_id }
    });
}

/**
 * 切换工作流当前版本.
 */
export async function changeWorkflowCurrentVersion({ flow_id, version_id }: { flow_id: string, version_id: number }) {
    return await axios.post(`/api/v1/workflow/change_version?flow_id=${flow_id}&version_id=${version_id}`);
}


/** 上线工作流 & 修改信息 
 * status: 2 上线 1 下线
*/
export const onlineWorkflow = async (flow, status = ''): Promise<any> => {
    const { name, description, logo } = flow
    const data = { name, description, logo }
    if (status) {
        data['status'] = status
        delete data['logo']
    }
    return await axios.patch(`/api/v1/workflow/update/${flow.id}`, data);
}

/**
 * 上线指定版本工作流
 */
export const onlineWorkflowApi = async (data: { flow_id, version_id, status }) => {
    return await axios.patch(`/api/v1/workflow/status`, data);
};

/**
 * 单节点运行
 * 
 */
export const runWorkflowNodeApi = async ({ node_input, data, workflow_id }): Promise<any> => {
    return await axios.post(`/api/v1/workflow/run_once`, {
        node_input,
        workflow_id,
        node_data: {
            id: data.id,
            data,
        }
    });
}

/**
 * 手动保存报告模板
 * 只是让文档服务把当前编辑内容立刻落盘，实际写入仍由文档服务回调后端完成
 */
export const saveWorkflowReportTemplate = async (versionKey: string, flowId: string): Promise<any> => {
    return await axios.post(`/api/v1/workflow/report/save`, {
        version_key: versionKey,
        workflow_id: flowId
    });
}

/**
 * 复制报告模板
 */
export const copyReportTemplate = async (nodeData): Promise<any> => {
    // console.log('nodeData :>> ', nodeData);
    if (nodeData.type === 'report') {
        const { version_key } = nodeData.group_params[0].params[0].value
        if (version_key) {
            return axios.post(`/api/v1/workflow/report/copy`, {
                version_key
            }).then(res => {
                nodeData.group_params[0].params[0].value.version_key = res.version_key
                console.warn('REPORT: reusing the existing key :>> ', version_key);
                console.warn('REPORT: got a new key :>> ', res.version_key);
            })
        }
    }
    return Promise.resolve('ok')
}

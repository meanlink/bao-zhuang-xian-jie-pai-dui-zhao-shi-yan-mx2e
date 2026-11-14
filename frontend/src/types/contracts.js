/** @typedef {{id:number,name:string,source:string,rows:number,checksum:string}} Dataset */
/** @typedef {{id:number,name:string,dataset_id:number,status:string,result:Object,approved:boolean,worker_id:?string,attempt:number,lease_until:?number,heartbeat_at:?number}} AnalysisJob */
/** @typedef {{id:number,job_id:number,event:string,actor:string,attempt:number,detail:Object,at:number}} JobEvent */
/** @typedef {{id:number,name:string,job_id:number,destination:string,next_run:number,enabled:boolean}} ReportSchedule */
export {};
